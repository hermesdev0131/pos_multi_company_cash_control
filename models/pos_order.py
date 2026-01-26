# -*- coding: utf-8 -*-

import logging
import qrcode
import base64
from io import BytesIO
from datetime import datetime
from pytz import UTC, timezone
from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    """
    Extension of pos.order model to support multi-company cash control.

    Overrides sync_from_ui to implement dynamic company switching
    for cash payments based on configured rules.

    Business Rules:
    - POS orders are assigned to a company at creation time (ticket-level, never split)
    - Each ticket belongs to one and only one company
    - First order of the day → ALWAYS fiscal company
    - System evaluates ticket by ticket, tracking running daily totals per POS config
    - While non-fiscal % < target → assign to non-fiscal company
    - Once non-fiscal % >= target → assign to fiscal company
    - Overshooting due to high-value tickets is accepted
    - No retroactive changes, no end-of-day rebalancing
    - Totals calculated from today's orders (not stored counters)
    """
    _inherit = 'pos.order'

    is_fiscal_order = fields.Boolean(
        string='Is Fiscal Order',
        compute='_compute_is_fiscal_order',
        store=True,
        search='_search_is_fiscal_order',
        help='True if this order was assigned to the fiscal company'
    )

    non_fiscal_qr_data = fields.Char(
        string='Non-Fiscal QR Data',
        compute='_compute_non_fiscal_qr_data',
        store=True,
        help='Base64-encoded QR code image for non-fiscal receipts'
    )

    @api.depends('company_id')
    def _compute_is_fiscal_order(self):
        """
        Compute whether this order belongs to a fiscal company.

        An order is considered fiscal if its company matches the fiscal_company_id
        of any active rule for the order's POS config.

        CRITICAL: Uses sudo() to avoid access errors when computing across companies.
        """
        for order in self.sudo():
            order.is_fiscal_order = False

            if not order.config_id:
                continue

            # Find the rule for this POS config
            rule = self.env['pos.cash.company.rule'].sudo().search([
                ('pos_config_id', '=', order.config_id.id),
                ('active', '=', True)
            ], limit=1, order='sequence')

            if rule and rule.fiscal_company_id:
                order.is_fiscal_order = (order.company_id.id == rule.fiscal_company_id.id)

    def _search_is_fiscal_order(self, operator, value):
        """
        Enable searching/filtering by is_fiscal_order field.

        Returns domain that filters orders based on whether they belong
        to fiscal companies according to active rules.
        """
        if operator not in ('=', '!='):
            raise ValidationError('Operator %s not supported for is_fiscal_order search' % operator)

        # Get all active rules
        rules = self.env['pos.cash.company.rule'].sudo().search([('active', '=', True)])

        fiscal_company_ids = rules.mapped('fiscal_company_id.id')

        # Build domain based on operator and value
        if (operator == '=' and value) or (operator == '!=' and not value):
            # Search for orders in fiscal companies
            return [('company_id', 'in', fiscal_company_ids)]
        else:
            # Search for orders NOT in fiscal companies
            return [('company_id', 'not in', fiscal_company_ids)]

    def _get_order_company_data(self):
        """
        Get company data dictionary for the order.

        Returns a dictionary with all company details needed for the receipt.
        Uses sudo() to access company across multi-company boundaries.
        """
        self.ensure_one()
        company = self.company_id.sudo()
        return {
            'id': company.id,
            'name': company.name or '',
            'street': company.street or '',
            'street2': company.street2 or '',
            'city': company.city or '',
            'zip': company.zip or '',
            'state_id': {
                'id': company.state_id.id,
                'name': company.state_id.name,
            } if company.state_id else False,
            'country_id': {
                'id': company.country_id.id,
                'name': company.country_id.name,
            } if company.country_id else False,
            'vat': company.vat or '',
            'phone': company.phone or '',
            'email': company.email or '',
        }

    @api.depends('company_id', 'config_id', 'name', 'date_order')
    def _compute_non_fiscal_qr_data(self):
        """
        Generate QR code for non-fiscal orders.

        QR code contains: Order reference | Company name | Timestamp
        Only generated for non-fiscal orders (fiscal orders get False).

        CRITICAL: Uses sudo() to avoid access errors when computing across companies.
        """
        for order in self.sudo():
            # Determine if fiscal order directly (don't depend on is_fiscal_order field)
            is_fiscal = False
            if order.config_id:
                rule = self.env['pos.cash.company.rule'].sudo().search([
                    ('pos_config_id', '=', order.config_id.id),
                    ('active', '=', True)
                ], limit=1, order='sequence')
                if rule and rule.fiscal_company_id:
                    is_fiscal = (order.company_id.id == rule.fiscal_company_id.id)

            if is_fiscal:
                # Fiscal orders don't get QR codes
                order.non_fiscal_qr_data = False
            else:
                # Non-fiscal orders get a QR code
                try:
                    # Build QR content
                    qr_content = f"{order.name}|{order.company_id.name}|{order.date_order}"

                    # Generate QR code image
                    qr = qrcode.QRCode(
                        version=1,
                        error_correction=qrcode.constants.ERROR_CORRECT_L,
                        box_size=10,
                        border=4
                    )
                    qr.add_data(qr_content)
                    qr.make(fit=True)

                    img = qr.make_image(fill_color="black", back_color="white")

                    # Convert to base64
                    buffer = BytesIO()
                    img.save(buffer, format='PNG')
                    img_str = base64.b64encode(buffer.getvalue()).decode()

                    order.non_fiscal_qr_data = img_str
                except Exception as e:
                    _logger.error(f"[POS MCC][QR] Failed to generate QR code for order {order.name}: {str(e)}")
                    order.non_fiscal_qr_data = False

    def _order_fields(self, ui_order):
        """
        Override to preserve company_id injection from sync_from_ui.

        CRITICAL: Without this override, the company_id we inject in sync_from_ui
        would NOT be mapped from the UI order data to the ORM field values.

        This method is called by the parent create() to extract field values
        from the UI order dictionary.
        """
        res = super()._order_fields(ui_order)

        # If we injected a company_id in sync_from_ui, preserve it here
        if 'company_id' in ui_order:
            res['company_id'] = ui_order['company_id']
            _logger.debug("[POS MCC][COMPANY] _order_fields preserving company_id: %s", ui_order['company_id'])

        return res

    @api.model
    def sync_from_ui(self, orders):
        """
        Override sync_from_ui to modify company before order creation.

        CRITICAL: This is the correct hook for Odoo 18 POS (NOT create_from_ui).

        This method is called when POS orders are synchronized from the frontend.
        It intercepts the order data and injects the appropriate company_id based on
        configured cash routing rules.

        Args:
            orders (list): List of order dictionaries from POS frontend

        Returns:
            dict: Result from parent sync_from_ui method
        """
        _logger.info("[POS MCC][COMPANY] sync_from_ui called with %d orders", len(orders))

        for order_data in orders:
            # Handle both formats: orders wrapped in {'data': ...} and direct dictionaries
            if isinstance(order_data, dict) and 'data' in order_data:
                ui_order = order_data['data']
            elif isinstance(order_data, dict):
                ui_order = order_data
            else:
                _logger.warning("[POS MCC][COMPANY] Unexpected order format: %s", type(order_data))
                continue
            order_name = ui_order.get('name', 'N/A')

            _logger.info("[POS MCC][COMPANY] Processing order: %s", order_name)

            # Guard 1: Skip returns/refunds (negative amounts)
            amount_total = ui_order.get('amount_total', 0)
            if amount_total < 0:
                _logger.info("[POS MCC][COMPANY] Skipping refund order")
                continue

            # Guard 2: Get session and config
            # NOTE: Odoo 18 uses 'session_id' not 'pos_session_id'
            session_id = ui_order.get('session_id')
            if not session_id:
                _logger.info("[POS MCC][COMPANY] No session_id found")
                continue

            # Use sudo to read session across companies
            pos_session = self.env['pos.session'].sudo().browse(session_id)
            if not pos_session or not pos_session.config_id:
                _logger.info("[POS MCC][COMPANY] POS session or config not found")
                continue

            pos_config = pos_session.config_id

            # Step 3: Find active rule for this POS config
            rule = self.env['pos.cash.company.rule'].sudo().search([
                ('pos_config_id', '=', pos_config.id),
                ('active', '=', True)
            ], limit=1, order='sequence')

            if not rule:
                _logger.info("[POS MCC][COMPANY] No active rule found for POS: %s", pos_config.name)
                continue

            # Step 4: Check for cash payment
            # NOTE: Odoo 18 uses 'payment_ids' not 'statement_ids'
            payment_ids = ui_order.get('payment_ids', [])
            if not payment_ids:
                _logger.info("[POS MCC][COMPANY] No payment statements found")
                continue

            # Determine which payment method IDs to check
            if rule.cash_payment_method_ids:
                target_payment_method_ids = set(rule.cash_payment_method_ids.ids)
            else:
                cash_methods = self.env['pos.payment.method'].sudo().search([
                    ('is_cash_count', '=', True)
                ])
                target_payment_method_ids = set(cash_methods.ids)

            # Check if any payment in the order matches our target cash methods
            has_cash_payment = False
            for payment in payment_ids:
                # payment is typically a tuple: (0, 0, {payment_data})
                if isinstance(payment, (list, tuple)) and len(payment) >= 3:
                    payment_data = payment[2]
                elif isinstance(payment, dict):
                    payment_data = payment
                else:
                    continue

                payment_method_id = payment_data.get('payment_method_id')
                if payment_method_id and payment_method_id in target_payment_method_ids:
                    has_cash_payment = True
                    break

            if not has_cash_payment:
                _logger.info("[POS MCC][COMPANY] No cash payment found in order")
                continue

            # Step 5: Get today's totals and make decision
            # CRITICAL: Pass POS session to ensure timezone consistency across all operations
            # _get_today_cash_totals() already uses sudo() internally, so we can call it directly
            try:
                totals = rule._get_today_cash_totals(session=pos_session)
            except Exception as e:
                _logger.error(f"[POS MCC][COMPANY] Error calling _get_today_cash_totals: {str(e)}")
                continue
            fiscal_total = totals['fiscal']
            non_fiscal_total = totals['non_fiscal']
            total_today = fiscal_total + non_fiscal_total

            # Calculate current non-fiscal ratio
            if total_today == 0.0:
                current_non_fiscal_ratio = 0.0
            else:
                current_non_fiscal_ratio = (non_fiscal_total / total_today) * 100.0

            # Make the decision using the rule's logic
            # CRITICAL: Pass POS session to ensure timezone consistency
            # decide_company_for_amount() uses _get_today_cash_totals() which already uses sudo() internally
            selected_company = rule.decide_company_for_amount(amount_total, session=pos_session)

            if selected_company:
                # INJECT COMPANY INTO ORDER DATA
                # This is the critical line that changes which company the order belongs to
                ui_order['company_id'] = selected_company.id

                # Mandatory logging with required format
                _logger.info(
                    "[POS MCC][COMPANY] Order: %s | Fiscal Total: %.2f | Non-Fiscal Total: %.2f | "
                    "Current Ratio: %.2f%% | Target: %.2f%% | Selected Company: %s (ID: %d) | "
                    "Amount: %.2f | Rule: '%s' | POS: '%s'",
                    order_name,
                    fiscal_total,
                    non_fiscal_total,
                    current_non_fiscal_ratio,
                    rule.target_non_fiscal_percentage,
                    selected_company.name,
                    selected_company.id,
                    amount_total,
                    rule.name,
                    pos_config.name
                )
            else:
                _logger.warning("[POS MCC][COMPANY] Rule returned no company for order: %s", order_name)

        # Call parent method to actually create the orders with modified company_id
        # Use sudo and appropriate company context for cross-company creation
        result = super(PosOrder, self.sudo()).sync_from_ui(orders)

        # WORKAROUND: Convert result to JSON and back to avoid access rights issues
        # when returning records created in different companies
        import json
        try:
            result_json = json.dumps(result)
            result = json.loads(result_json)
        except (TypeError, ValueError):
            # If JSON serialization fails, return as-is
            pass

        # Enrich result with company data and custom fields for frontend receipt
        if result and isinstance(result, list):
            for order_data in result:
                if isinstance(order_data, dict) and 'id' in order_data:
                    order = self.sudo().browse(order_data['id'])
                    if order.exists():
                        order_data['company_data'] = order._get_order_company_data()
                        order_data['is_fiscal_order'] = order.is_fiscal_order
                        order_data['non_fiscal_qr_data'] = order.non_fiscal_qr_data or False
                        _logger.debug(
                            "[POS MCC][RECEIPT] Enriched order %s: is_fiscal=%s, company=%s",
                            order.name, order.is_fiscal_order, order.company_id.name
                        )

        return result

    def read_pos_data(self, config_id, data_type):
        """
        Override to handle reading orders created in different companies.

        When orders are created with different company_ids than the session,
        we need sudo access to read them back.
        """
        _logger.debug("[POS MCC][COMPANY] read_pos_data called for config: %s, type: %s",
                     config_id, data_type)

        if data_type == 'pos.order':
            # Use sudo to read orders across all companies
            return super(PosOrder, self.sudo()).read_pos_data(config_id, data_type)

        return super().read_pos_data(config_id, data_type)

    def _complete_values_from_session(self, session, values):
        """
        Override to prevent session company from overwriting our injected company_id
        and to set date_order using logged-in user's timezone.

        CRITICAL OVERRIDE: Without this, Odoo's base code calls:
            values.setdefault('company_id', session.company_id.id)

        This would overwrite the company_id we carefully injected in sync_from_ui,
        causing all orders to use the session's company instead of our routing logic.

        This override preserves our injected company_id by restoring it after
        the parent method completes.

        Also sets date_order using logged-in user's timezone to ensure consistency
        across all POS operations (creating orders, filtering, searching).
        """
        # Capture our injected company_id BEFORE parent processes it
        injected_company_id = values.get('company_id')
        
        # CRITICAL: Set date_order using logged-in user's timezone
        # This ensures all POS operations use the same timezone (user's timezone)
        # The frontend sends date_order in browser's timezone, but we override it
        # to use the logged-in user's timezone for consistency
        user = self.env.user
        
        if user and user.tz:
            try:
                user_tz = timezone(user.tz)
                # Get current time in user's timezone
                now_user_tz = datetime.now(user_tz)
                # Convert to UTC (Odoo stores datetimes in UTC)
                now_utc = now_user_tz.astimezone(UTC)
                # Remove timezone info (Odoo stores naive datetimes in UTC)
                values['date_order'] = now_utc.replace(tzinfo=None)
            except Exception as e:
                _logger.warning(f"[POS MCC][TIMEZONE] Failed to set date_order with user timezone {user.tz}: {str(e)}")
                # Fall back to default behavior (use current UTC time)
                if 'date_order' not in values:
                    values['date_order'] = fields.Datetime.now()
        else:
            # User timezone not set, use default (UTC)
            if 'date_order' not in values:
                values['date_order'] = fields.Datetime.now()

        # Call parent (which may overwrite company_id with session.company_id)
        res = super()._complete_values_from_session(session, values)

        # If we had injected a company_id and it got overwritten, restore it
        if injected_company_id and res.get('company_id') != injected_company_id:
            _logger.info(
                "[POS MCC][COMPANY] _complete_values_from_session: Restoring company_id %d "
                "(was overwritten with session company %d)",
                injected_company_id,
                res.get('company_id')
            )
            res['company_id'] = injected_company_id

        return res
