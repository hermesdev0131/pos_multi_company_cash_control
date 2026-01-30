# -*- coding: utf-8 -*-

import logging
import qrcode
import base64
from io import BytesIO
from datetime import datetime
from pytz import UTC, timezone
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

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

    order_company_data = fields.Json(
        string='Order Company Data',
        compute='_compute_order_company_data',
        store=True,
        help='JSON with company details for receipt display'
    )

    @api.depends('company_id')
    def _compute_order_company_data(self):
        """
        Compute and store company data for the order.
        This is stored so it's available to the POS frontend for receipt display.
        """
        for order in self.sudo():
            company = order.company_id
            if company:
                order.order_company_data = {
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
                    'website': company.website or '',
                }
            else:
                order.order_company_data = False

    @api.depends('company_id')
    def _compute_is_fiscal_order(self):
        """
        Compute whether this order belongs to a fiscal company.

        An order is considered fiscal if:
        - No active rule exists for the POS config (default to fiscal)
        - Its company matches the fiscal_company_id of the active rule

        CRITICAL: Uses sudo() to avoid access errors when computing across companies.
        """
        for order in self.sudo():
            # Default to True (fiscal) - orders without rules are fiscal
            order.is_fiscal_order = True

            if not order.config_id:
                continue

            # Find the rule for this POS config
            rule = self.env['pos.cash.company.rule'].sudo().search([
                ('pos_config_id', '=', order.config_id.id),
                ('is_enabled', '=', True)
            ], limit=1, order='sequence')

            if rule and rule.fiscal_company_id and rule.non_fiscal_company_id:
                # Has active rule - check if order belongs to fiscal or non-fiscal company
                order.is_fiscal_order = (order.company_id.id == rule.fiscal_company_id.id)
            # else: keep default True (no rule means fiscal)

    def _search_is_fiscal_order(self, operator, value):
        """
        Enable searching/filtering by is_fiscal_order field.

        Returns domain that filters orders based on whether they belong
        to fiscal companies according to active rules.
        """
        if operator not in ('=', '!='):
            raise ValidationError('Operator %s not supported for is_fiscal_order search' % operator)

        # Get all active rules
        rules = self.env['pos.cash.company.rule'].sudo().search([('is_enabled', '=', True)])

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
        Only generated for non-fiscal orders (fiscal orders and orders without rules get False).

        CRITICAL: Uses sudo() to avoid access errors when computing across companies.
        """
        for order in self.sudo():
            # Default to True (fiscal) - orders without rules don't get QR codes
            is_fiscal = True
            if order.config_id:
                rule = self.env['pos.cash.company.rule'].sudo().search([
                    ('pos_config_id', '=', order.config_id.id),
                    ('is_enabled', '=', True)
                ], limit=1, order='sequence')
                if rule and rule.fiscal_company_id and rule.non_fiscal_company_id:
                    # Has active rule - check if fiscal or non-fiscal
                    is_fiscal = (order.company_id.id == rule.fiscal_company_id.id)
                # else: keep default True (no rule means fiscal, no QR code)

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
                ('is_enabled', '=', True)
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

        _logger.debug("[POS MCC][RECEIPT] sync_from_ui result type: %s", type(result))

        # WORKAROUND: Convert result to JSON and back to avoid access rights issues
        # when returning records created in different companies
        import json
        try:
            result_json = json.dumps(result)
            result = json.loads(result_json)
        except (TypeError, ValueError) as e:
            _logger.warning("[POS MCC][RECEIPT] JSON serialization failed: %s", str(e))
            pass

        # Enrich result with company data and custom fields for frontend receipt
        try:
            # Handle different result structures
            order_list = []
            if isinstance(result, list):
                order_list = result
            elif isinstance(result, dict):
                # Some Odoo versions return a dict with orders inside
                if 'orders' in result:
                    order_list = result.get('orders', [])
                elif 'id' in result:
                    # Single order as dict
                    order_list = [result]

            for order_data in order_list:
                if isinstance(order_data, dict) and 'id' in order_data:
                    order = self.sudo().browse(order_data['id'])
                    if order.exists():
                        order_data['company_data'] = order._get_order_company_data()
                        order_data['is_fiscal_order'] = order.is_fiscal_order
                        order_data['non_fiscal_qr_data'] = order.non_fiscal_qr_data or False
                        _logger.info(
                            "[POS MCC][RECEIPT] Enriched order %s: is_fiscal=%s, company=%s",
                            order.name, order.is_fiscal_order, order.company_id.name
                        )
        except Exception as e:
            _logger.error("[POS MCC][RECEIPT] Error enriching result: %s", str(e))

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

    def read(self, fields=None, load='_classic_read'):
        """
        Override read to use sudo() for multi-company orders.

        This is necessary when reading orders that belong to a different
        company than the user's current company.
        """
        try:
            return super().read(fields=fields, load=load)
        except Exception as e:
            _logger.debug("[POS MCC][COMPANY] read() using sudo due to access error: %s", str(e))
            return super(PosOrder, self.sudo()).read(fields=fields, load=load)

    def write(self, vals):
        """
        Override write to use sudo() for multi-company orders.

        This is necessary when the POS frontend tries to update orders
        that belong to a different company than the user's current company.
        """
        # Always use sudo for POS orders to avoid multi-company access issues
        # This is safe because we're within the POS context
        try:
            # Check if any order is in a different company than current
            current_company_id = self.env.company.id
            needs_sudo = any(
                order.sudo().company_id.id != current_company_id
                for order in self
            )
            if needs_sudo:
                _logger.debug("[POS MCC][COMPANY] write() using sudo for cross-company order update")
                return super(PosOrder, self.sudo()).write(vals)
        except Exception as e:
            _logger.debug("[POS MCC][COMPANY] write() using sudo due to access check error: %s", str(e))
            return super(PosOrder, self.sudo()).write(vals)

        return super().write(vals)

    def action_pos_order_paid(self):
        """
        Override to use sudo() for multi-company orders.

        This method is called when an order is marked as paid.
        """
        try:
            if self.sudo().company_id.id != self.env.company.id:
                _logger.debug("[POS MCC][COMPANY] action_pos_order_paid using sudo for company %s", self.sudo().company_id.name)
                return super(PosOrder, self.sudo()).action_pos_order_paid()
        except Exception as e:
            _logger.debug("[POS MCC][COMPANY] action_pos_order_paid using sudo due to error: %s", str(e))
            return super(PosOrder, self.sudo()).action_pos_order_paid()
        return super().action_pos_order_paid()

    def action_pos_order_invoice(self):
        """
        Override to use sudo() for multi-company orders.

        This method is called when generating an invoice for the order.
        When the order belongs to a different company than the session, the base
        invoice generation code accesses order.session_id, which can fail due to
        record rules preventing cross-company access.
        
        With the record rules for pos.session added in ir_rule.xml, sudo() should
        be sufficient to access both the order (in order company) and the session
        (in session company).
        """
        try:
            # Check if order is in a different company than current context
            order_company_id = self.sudo().company_id.id
            current_company_id = self.env.company.id
            
            if order_company_id != current_company_id:
                _logger.info(
                    "[POS MCC][COMPANY] action_pos_order_invoice: Order company %s != current company %s, "
                    "using sudo to allow cross-company access",
                    order_company_id, current_company_id
                )
                # Use sudo to bypass access rights - record rules allow cross-company access
                return super(PosOrder, self.sudo()).action_pos_order_invoice()
        except Exception as e:
            _logger.warning(
                "[POS MCC][COMPANY] action_pos_order_invoice using sudo due to error: %s",
                str(e)
            )
            # On error, use sudo to ensure access
            return super(PosOrder, self.sudo()).action_pos_order_invoice()
        
        return super().action_pos_order_invoice()

    def _generate_pos_order_invoice(self):
        """
        Override to use session company for all invoice operations.

        The base method uses order.company_id in several places:
        - _post() with_company(order.company_id)
        - _apply_invoice_payments() with_company(self.company_id)
        Since fiscal/non-fiscal companies have no accounting setup,
        we must use the session company for all of these.
        """
        moves = self.env['account.move']

        for order in self.sudo():
            if order.account_move:
                moves += order.account_move
                continue

            if not order.partner_id:
                raise UserError('Please provide a partner for the sale.')

            session_company = order.session_id.company_id

            move_vals = order._prepare_invoice_vals()
            new_move = order._create_invoice(move_vals)

            order.state = 'invoiced'
            # FIXED: Use session company instead of order.company_id
            new_move.sudo().with_company(session_company).with_context(
                **order._get_invoice_post_context())._post()

            moves += new_move
            payment_moves = order._apply_invoice_payments(
                order.session_id.state == 'closed')

            if self.env.context.get('generate_pdf', True):
                new_move.with_context(skip_invoice_sync=True)._generate_and_send()

            if order.session_id.state == 'closed':
                order._create_misc_reversal_move(payment_moves)

        if not moves:
            return {}

        return {
            'name': 'Customer Invoice',
            'view_mode': 'form',
            'view_id': self.env.ref('account.view_move_form').id,
            'res_model': 'account.move',
            'context': "{'move_type':'out_invoice'}",
            'type': 'ir.actions.act_window',
            'target': 'current',
            'res_id': moves and moves.ids[0] or False,
        }

    def _apply_invoice_payments(self, is_reverse=False):
        """
        Override to use session company for payment move creation and reconciliation.

        The base method uses self.company_id (stored field = order company) for:
        - Resolving partner receivable account
        - Creating payment moves via pos.payment._create_payment_moves()
        - Reconciling invoice lines
        Since fiscal/non-fiscal companies have no accounting setup,
        we replicate the base logic using session_company instead of self.company_id.
        """
        session_company = self.session_id.company_id

        _logger.info(
            "[POS MCC][PAYMENT] _apply_invoice_payments: Order %s "
            "(order company=%s) using session company=%s",
            self.name,
            self.company_id.name if self.company_id else 'None',
            session_company.name,
        )

        # Base logic replicated with session_company instead of self.company_id
        receivable_account = self.env["res.partner"]._find_accounting_partner(
            self.partner_id
        ).with_company(session_company).property_account_receivable_id

        payment_moves = self.payment_ids.sudo().with_company(
            session_company
        )._create_payment_moves(is_reverse)

        if receivable_account.reconcile:
            invoice_receivables = self.account_move.line_ids.filtered(
                lambda line: line.account_id == receivable_account
                and not line.reconciled
            )
            if invoice_receivables:
                credit_line_ids = payment_moves._context.get('credit_line_ids', None)
                payment_receivables = payment_moves.mapped('line_ids').filtered(
                    lambda line: (
                        (credit_line_ids and line.id in credit_line_ids)
                        or (not credit_line_ids
                            and line.account_id == receivable_account
                            and line.partner_id)
                    )
                )
                (invoice_receivables | payment_receivables).sudo().with_company(
                    session_company
                ).reconcile()

        return payment_moves

    def _prepare_invoice_vals(self):
        """
        Override to ensure invoice uses the POS session company for accounting.

        Orders are routed to fiscal/non-fiscal companies for categorization,
        but those companies have no accounting setup (no journals, accounts, taxes).
        All invoicing must use the session company which owns the POS and has
        the full accounting infrastructure.
        """
        session_company = self.session_id.company_id

        _logger.info(
            "[POS MCC][INVOICE] _prepare_invoice_vals: Order %s (order company=%s, session company=%s)",
            self.name,
            self.company_id.name if self.company_id else 'None',
            session_company.name if session_company else 'None',
        )

        # Call parent with session company context so all records
        # (journal, accounts, taxes, fiscal position) resolve correctly
        vals = super(PosOrder, self.with_company(session_company))._prepare_invoice_vals()

        # Ensure invoice is created in session company
        vals['company_id'] = session_company.id

        return vals

    def _create_invoice(self, move_vals):
        """
        Override to create invoice in the POS session company context.

        The base method uses with_company(self.company_id) which would resolve
        to the fiscal/non-fiscal company that has no accounting setup.
        We switch to the session company which has all the accounting infrastructure.
        """
        self.ensure_one()
        session_company = self.session_id.company_id

        # Ensure invoice is created in session company
        move_vals['company_id'] = session_company.id

        _logger.info(
            "[POS MCC][INVOICE] _create_invoice: Order %s (order company=%s) -> invoice in session company=%s",
            self.name,
            self.company_id.name if self.company_id else 'None',
            session_company.name,
        )

        # Call parent with session company context so the base method's
        # with_company(self.company_id) uses session company
        return super(PosOrder, self.with_company(session_company))._create_invoice(move_vals)

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


class PosOrderLine(models.Model):
    """
    Extension of pos.order.line model to handle income account lookup
    for multi-company orders.

    When orders are routed to fiscal/non-fiscal companies, products may not
    have income accounts configured for those companies. This override
    provides fallback logic to use the session company's income account
    if the order company doesn't have one configured.
    """
    _inherit = 'pos.order.line'

    def _prepare_base_line_for_taxes_computation(self):
        """
        Override to use the POS session company for account and tax resolution.

        Orders are categorized under fiscal/non-fiscal companies that have no
        accounting setup. All account and tax lookups must use the session company.
        """
        self.ensure_one()
        session_company = self.order_id.session_id.company_id

        # Use session company for all accounting lookups
        line = self.with_company(session_company)

        # Call parent with session company context
        return super(PosOrderLine, line)._prepare_base_line_for_taxes_computation()
