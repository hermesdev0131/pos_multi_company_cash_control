# -*- coding: utf-8 -*-

import logging
import qrcode
import base64
from io import BytesIO
from datetime import datetime
from pytz import UTC, timezone
from odoo import api, fields, models
from odoo.exceptions import ValidationError, UserError
from odoo import _

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
        Override to ensure session access works for multi-company orders.
        
        The base method accesses order.session_id which can fail when the order
        is in a different company than the session. We ensure the session is
        accessible by using sudo() when there's a company mismatch.
        """
        # Check if any order's session is in a different company than the order
        # This happens when orders are routed to fiscal/non-fiscal companies
        needs_sudo = False
        for order in self.sudo():
            order_company_id = order.company_id.id
            session_company_id = order.session_id.company_id.id if order.session_id else None
            
            if session_company_id and order_company_id != session_company_id:
                needs_sudo = True
                _logger.info(
                    "[POS MCC][COMPANY] _generate_pos_order_invoice: Order %s (company %s) has session "
                    "in different company %s, using sudo",
                    order.name, order_company_id, session_company_id
                )
                break
        
        if needs_sudo:
            # Use sudo to bypass record rules and allow cross-company session access
            return super(PosOrder, self.sudo())._generate_pos_order_invoice()
        
        # If no company mismatch, use normal flow
        return super()._generate_pos_order_invoice()

    def _prepare_invoice_vals(self):
        """
        Override to explicitly set company_id and validate journal/partner compatibility
        for multi-company orders.
        
        When orders are routed to fiscal/non-fiscal companies, we need to ensure:
        1. The invoice has the correct company_id
        2. The journal exists in the target company
        3. The partner is compatible with the target company (shared or exists in target company)
        
        This prevents "Incompatible companies" errors when creating invoices.
        """
        vals = super()._prepare_invoice_vals()
        
        # Explicitly set company_id to ensure invoice lines inherit it correctly
        # This is critical for multi-company scenarios where orders are routed
        # to different companies than the session
        if not self.company_id:
            _logger.warning(
                "[POS MCC][COMPANY] _prepare_invoice_vals: Order %s has no company_id!",
                self.name
            )
            return vals
        
        target_company = self.company_id
        vals['company_id'] = target_company.id
        
        _logger.debug(
            "[POS MCC][COMPANY] _prepare_invoice_vals: Setting company_id=%d (%s) for invoice",
            target_company.id,
            target_company.name
        )
        
        # CRITICAL: Validate and fix journal_id to ensure it's compatible with target company
        journal_id = vals.get('journal_id')
        if journal_id:
            journal = self.env['account.journal'].sudo().browse(journal_id)
            if journal.exists():
                # Check if journal is compatible with target company
                if journal.company_id.id != target_company.id:
                    _logger.warning(
                        "[POS MCC][INVOICE] Journal '%s' (id:%d) belongs to company '%s', "
                        "but order belongs to company '%s'. Searching for compatible journal...",
                        journal.name,
                        journal.id,
                        journal.company_id.name,
                        target_company.name
                    )
                    
                    # Try to find a journal with the same code in the target company
                    compatible_journal = self.env['account.journal'].sudo().search([
                        ('code', '=', journal.code),
                        ('company_id', '=', target_company.id),
                        ('type', '=', journal.type),
                    ], limit=1)
                    
                    if compatible_journal:
                        vals['journal_id'] = compatible_journal.id
                        _logger.info(
                            "[POS MCC][INVOICE] Found compatible journal '%s' (id:%d) in target company '%s'",
                            compatible_journal.name,
                            compatible_journal.id,
                            target_company.name
                        )
                    else:
                        # Try to find any sales journal in target company
                        compatible_journal = self.env['account.journal'].sudo().search([
                            ('company_id', '=', target_company.id),
                            ('type', '=', 'sale'),
                        ], limit=1)
                        
                        if compatible_journal:
                            vals['journal_id'] = compatible_journal.id
                            _logger.warning(
                                "[POS MCC][INVOICE] No journal with code '%s' found in target company. "
                                "Using default sales journal '%s' (id:%d) instead.",
                                journal.code,
                                compatible_journal.name,
                                compatible_journal.id
                            )
                        else:
                            _logger.error(
                                "[POS MCC][INVOICE] No compatible sales journal found in target company '%s'. "
                                "Invoice creation may fail!",
                                target_company.name
                            )
        
        # CRITICAL: Validate and fix partner_id to ensure it's compatible with target company
        partner_id = vals.get('partner_id')
        if partner_id:
            partner = self.env['res.partner'].sudo().browse(partner_id)
            if partner.exists():
                # Check if partner is compatible with target company
                # Partners can be:
                # 1. Shared (company_id=False or company_ids empty) - accessible across all companies
                # 2. Company-specific (company_id set or company_ids contains specific companies)
                
                is_shared = not partner.company_id and not partner.company_ids
                is_in_target_company = (
                    partner.company_id == target_company or
                    target_company in partner.company_ids
                )
                
                if not is_shared and not is_in_target_company:
                    _logger.warning(
                        "[POS MCC][INVOICE] Partner '%s' (id:%d) belongs to company '%s', "
                        "but order belongs to company '%s'. Searching for compatible partner...",
                        partner.name,
                        partner.id,
                        partner.company_id.name if partner.company_id else 'Unknown',
                        target_company.name
                    )
                    
                    # For anonymous/final customers, try to find equivalent in target company
                    if 'Consumidor Final' in partner.name or 'Anónimo' in partner.name or 'Final' in partner.name:
                        # Look for anonymous customer in target company (shared or company-specific)
                        anonymous_partner = self.env['res.partner'].sudo().search([
                            ('name', 'ilike', partner.name),
                            '|',
                            ('company_id', '=', False),
                            ('company_id', '=', target_company.id),
                        ], limit=1)
                        
                        if not anonymous_partner:
                            # Try broader search
                            anonymous_partner = self.env['res.partner'].sudo().search([
                                '|',
                                ('name', 'ilike', 'Consumidor Final'),
                                ('name', 'ilike', 'Anónimo'),
                                '|',
                                ('company_id', '=', False),
                                ('company_id', '=', target_company.id),
                            ], limit=1)
                        
                        if anonymous_partner:
                            vals['partner_id'] = anonymous_partner.id
                            _logger.info(
                                "[POS MCC][INVOICE] Using compatible partner '%s' (id:%d) for target company",
                                anonymous_partner.name,
                                anonymous_partner.id
                            )
                        else:
                            # Try to use a shared partner or raise error
                            shared_partner = self.env['res.partner'].sudo().search([
                                ('company_id', '=', False),
                                ('is_company', '=', partner.is_company),
                            ], limit=1)
                            
                            if shared_partner:
                                vals['partner_id'] = shared_partner.id
                                _logger.warning(
                                    "[POS MCC][INVOICE] Using shared partner '%s' (id:%d) as fallback",
                                    shared_partner.name,
                                    shared_partner.id
                                )
                            else:
                                _logger.error(
                                    "[POS MCC][INVOICE] No compatible partner found for target company '%s'. "
                                    "Invoice creation may fail!",
                                    target_company.name
                                )
                else:
                    # Partner is shared or already in target company - no action needed
                    _logger.debug(
                        "[POS MCC][INVOICE] Partner '%s' is compatible with target company (shared=%s, in_company=%s)",
                        partner.name,
                        is_shared,
                        is_in_target_company
                    )
        
        return vals

    def _create_invoice(self, move_vals):
        """
        Override to ensure correct company context when creating invoices for multi-company orders.
        
        When orders are routed to fiscal/non-fiscal companies, we need to ensure
        the invoice is created with the correct company context, especially when
        using sudo() to bypass access rights. This ensures invoice lines have
        access to company.currency_id.
        """
        self.ensure_one()
        
        # Ensure company_id is set in move_vals (in case it wasn't set in _prepare_invoice_vals)
        if 'company_id' not in move_vals:
            # Get company_id from order (use sudo to ensure access)
            order_company_id = self.sudo().company_id.id if self.sudo().company_id else None
            if order_company_id:
                move_vals['company_id'] = order_company_id
                _logger.debug(
                    "[POS MCC][COMPANY] _create_invoice: Adding company_id=%d to move_vals",
                    order_company_id
                )
            else:
                _logger.warning(
                    "[POS MCC][COMPANY] _create_invoice: Order %s has no company_id!",
                    self.name
                )
        
        # Get company_id from move_vals or order
        company_id = move_vals.get('company_id') or (self.sudo().company_id.id if self.sudo().company_id else None)
        
        # Validate company exists and has currency
        if company_id:
            company = self.env['res.company'].sudo().browse(company_id)
            if not company.exists():
                raise UserError(_(
                    "Cannot create invoice for order %s: company with id %d does not exist.",
                    self.name, company_id
                ))
            
            if not company.currency_id:
                raise UserError(_(
                    "Cannot create invoice for order %s: company '%s' does not have a currency configured. "
                    "Please configure a currency for this company.",
                    self.name, company.name
                ))
        
        # Call parent method - it will use with_company() which should work correctly
        # But we ensure company_id is in move_vals so invoice lines inherit it
        return super()._create_invoice(move_vals)

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
        Override to handle missing income accounts in multi-company scenarios.

        When an order is routed to a different company (fiscal/non-fiscal),
        the product might not have an income account configured for that company.
        This method provides fallback logic:
        1. Try to get income account from order's company
        2. If not found, try session company (original company)
        3. If still not found, use journal default account
        4. If all fail, raise a more descriptive error
        """
        self.ensure_one()
        commercial_partner = self.order_id.partner_id.commercial_partner_id
        fiscal_position = self.order_id.fiscal_position_id
        
        order_company = self.order_id.company_id
        
        # Try order company first (normal flow)
        line = self.with_company(order_company)
        account = line.product_id._get_product_accounts()['income']
        
        # CRITICAL: Ensure account belongs to order company
        # Filter by company domain to ensure compatibility
        if account:
            account_domain = account._check_company_domain(order_company)
            account_filtered = account.filtered_domain(account_domain) if account_domain else account
            if not account_filtered or account_filtered.company_ids and order_company not in account_filtered.company_ids:
                _logger.warning(
                    "[POS MCC][ACCOUNT] Account '%s' (id:%d) does not belong to order company '%s' (id:%d). "
                    "Will try to find equivalent account or use fallback.",
                    account.name,
                    account.id,
                    order_company.name,
                    order_company.id
                )
                account = None  # Reset to try fallbacks
        
        # If no account in order company, try session company as fallback
        # But we'll need to find an equivalent account in order company
        if not account and self.order_id.session_id:
            session_company = self.order_id.session_id.company_id
            if session_company.id != order_company.id:
                _logger.info(
                    "[POS MCC][ACCOUNT] Product '%s' (id:%d) has no income account in order company '%s' (id:%d). "
                    "Trying session company '%s' (id:%d) as fallback.",
                    line.product_id.name,
                    line.product_id.id,
                    order_company.name,
                    order_company.id,
                    session_company.name,
                    session_company.id
                )
                line_session = self.with_company(session_company)
                session_account = line_session.product_id._get_product_accounts()['income']
                if session_account:
                    # Try to find equivalent account in order company by name or code
                    account_search = self.env['account.account'].sudo().search([
                        ('company_ids', 'in', [order_company.id]),
                        ('account_type', '=', 'income'),
                        '|',
                        ('name', '=', session_account.name),
                        ('code', '=', session_account.code),
                    ], limit=1)
                    
                    if account_search:
                        account = account_search
                        _logger.info(
                            "[POS MCC][ACCOUNT] Found equivalent account '%s' (id:%d) in order company '%s' "
                            "matching session company account '%s' (id:%d).",
                            account.name,
                            account.id,
                            order_company.name,
                            session_account.name,
                            session_account.id
                        )
                    else:
                        _logger.warning(
                            "[POS MCC][ACCOUNT] No equivalent account found in order company '%s' for "
                            "session company account '%s' (id:%d). Will use journal default.",
                            order_company.name,
                            session_account.name,
                            session_account.id
                        )
        
        # Fallback to journal default account (should be in order company)
        if not account:
            journal_account = self.order_id.config_id.journal_id.default_account_id
            if journal_account:
                # Verify journal account belongs to order company
                journal_account_domain = journal_account._check_company_domain(order_company)
                journal_account_filtered = journal_account.filtered_domain(journal_account_domain) if journal_account_domain else journal_account
                if journal_account_filtered and (not journal_account_filtered.company_ids or order_company in journal_account_filtered.company_ids):
                    account = journal_account_filtered
                    _logger.info(
                        "[POS MCC][ACCOUNT] Using journal default account '%s' (id:%d) for product '%s'.",
                        account.name,
                        account.id,
                        line.product_id.name
                    )
                else:
                    _logger.warning(
                        "[POS MCC][ACCOUNT] Journal default account '%s' (id:%d) does not belong to order company '%s'. "
                        "Cannot use as fallback.",
                        journal_account.name,
                        journal_account.id,
                        order_company.name
                    )
        
        # Final fallback: Try to find ANY income account in order company
        if not account:
            _logger.info(
                "[POS MCC][ACCOUNT] No account found through normal methods. "
                "Searching for any income account in order company '%s' (id:%d).",
                order_company.name,
                order_company.id
            )
            # Search in order company context to ensure we find accounts
            fallback_account = self.env['account.account'].sudo().with_company(order_company).search([
                ('company_ids', 'in', [order_company.id]),
                ('account_type', '=', 'income'),
                ('deprecated', '=', False),
            ], limit=1, order='code')
            
            # If still not found, try without company_ids filter (in case of shared accounts)
            if not fallback_account:
                _logger.debug(
                    "[POS MCC][ACCOUNT] No income account found with company_ids filter. "
                    "Trying broader search in order company context."
                )
                fallback_account = self.env['account.account'].sudo().with_company(order_company).search([
                    ('account_type', '=', 'income'),
                    ('deprecated', '=', False),
                ], limit=1, order='code')
                # Verify it's accessible in order company
                if fallback_account:
                    account_domain = fallback_account._check_company_domain(order_company)
                    if account_domain:
                        fallback_account = fallback_account.filtered_domain(account_domain)
                        if not fallback_account:
                            fallback_account = self.env['account.account']  # Reset if not compatible
            
            if fallback_account:
                account = fallback_account
                _logger.warning(
                    "[POS MCC][ACCOUNT] Using fallback income account '%s' (id:%d, code:%s) from order company '%s' "
                    "for product '%s'. This account may not be the correct one - please configure the product's "
                    "income account properly.",
                    account.name,
                    account.id,
                    account.code or 'N/A',
                    order_company.name,
                    line.product_id.name
                )
        
        # If no account found after all fallbacks, use None
        # The account_id is not strictly required for tax computation - it's only used for accounting grouping
        # The invoice line creation will automatically set account_id via _compute_account_id() 
        # which uses: product income account -> partner account -> journal default account
        if not account:
            _logger.info(
                "[POS MCC][ACCOUNT] No income account found for product '%s' (id:%d) in order company '%s' (id:%d). "
                "Proceeding without account_id - invoice line creation will set it automatically via _compute_account_id().",
                line.product_id.name,
                line.product_id.id,
                order_company.name,
                order_company.id
            )
            # Set account to None - tax computation doesn't strictly require it
            # Invoice line will get account via _compute_account_id() automatically
            account = None

        # CRITICAL: Final check - ensure account is compatible with order company (if account exists)
        # This prevents "Incompatible companies" error
        if account:
            account_domain = account._check_company_domain(order_company)
            account_filtered = account.filtered_domain(account_domain) if account_domain else account
            if not account_filtered or (account_filtered.company_ids and order_company not in account_filtered.company_ids):
                # Account is not compatible - set to None and let invoice line handle it
                _logger.warning(
                    "[POS MCC][ACCOUNT] Account '%s' (id:%d) is not compatible with order company '%s'. "
                    "Setting to None - invoice line will set account automatically.",
                    account.name,
                    account.id,
                    order_company.name
                )
                account = None
            else:
                account = account_filtered

            # Apply fiscal position mapping if account exists
            if account and fiscal_position:
                account = fiscal_position.map_account(account)
        else:
            # No account - fiscal position mapping not needed
            # Invoice line creation will handle account assignment via _compute_account_id()
            pass

        # CRITICAL: Filter taxes by order's company to avoid "Incompatible companies" error
        # When orders are routed to fiscal/non-fiscal companies, taxes from session company
        # may not be compatible with the order company
        order_company = self.order_id.company_id
        tax_ids = line.tax_ids_after_fiscal_position
        
        # Filter taxes to only include those compatible with order's company
        if tax_ids:
            # Use _filter_taxes_by_company which handles company hierarchy
            tax_ids_filtered = tax_ids._filter_taxes_by_company(order_company)
            
            # If no taxes found in order company, try to get taxes from product in order company context
            if not tax_ids_filtered:
                _logger.info(
                    "[POS MCC][TAX] No compatible taxes found in order company '%s' (id:%d) for product '%s' (id:%d). "
                    "Original taxes: %s. Trying to get taxes from product in order company context.",
                    order_company.name,
                    order_company.id,
                    line.product_id.name,
                    line.product_id.id,
                    [t.name for t in tax_ids]
                )
                
                # Get product taxes in order company context
                product_in_order_company = line.product_id.with_company(order_company)
                product_taxes = product_in_order_company.taxes_id.filtered_domain(
                    self.env['account.tax']._check_company_domain(order_company)
                )
                
                # Apply fiscal position if exists
                if product_taxes and fiscal_position:
                    product_taxes = fiscal_position.map_tax(product_taxes)
                
                if product_taxes:
                    tax_ids_filtered = product_taxes._filter_taxes_by_company(order_company)
                    _logger.info(
                        "[POS MCC][TAX] Using taxes from product in order company: %s",
                        [t.name for t in tax_ids_filtered]
                    )
            
            # If still no taxes, try session company as fallback (with proper company context)
            if not tax_ids_filtered and self.order_id.session_id:
                session_company = self.order_id.session_id.company_id
                if session_company.id != order_company.id:
                    _logger.info(
                        "[POS MCC][TAX] No taxes found in order company. Trying session company '%s' (id:%d) as fallback.",
                        session_company.name,
                        session_company.id
                    )
                    # Use original taxes but ensure they're accessible in session company context
                    tax_ids_filtered = tax_ids._filter_taxes_by_company(session_company)
                    if tax_ids_filtered:
                        _logger.info(
                            "[POS MCC][TAX] Using taxes from session company: %s (Note: These will be used with order company context)",
                            [t.name for t in tax_ids_filtered]
                        )
            
            tax_ids = tax_ids_filtered if tax_ids_filtered else tax_ids
            
            # Final check: ensure all taxes are compatible with order company
            # This prevents the "Incompatible companies" error
            if tax_ids:
                incompatible_taxes = tax_ids.filtered(
                    lambda t: t.company_id and t.company_id.id != order_company.id
                )
                if incompatible_taxes:
                    _logger.warning(
                        "[POS MCC][TAX] Removing incompatible taxes: %s (belong to different company than order)",
                        [t.name for t in incompatible_taxes]
                    )
                    tax_ids = tax_ids - incompatible_taxes

        is_refund_order = line.order_id.amount_total < 0.0
        is_refund_line = line.qty * line.price_unit < 0

        product_name = line.product_id \
            .with_context(lang=line.order_id.partner_id.lang or self.env.user.lang) \
            .get_product_multiline_description_sale()

        # Convert None account to empty recordset for tax computation
        # account_id is optional - invoice line will set it automatically via _compute_account_id()
        account_for_tax = account if account else self.env['account.account']
        
        return {
            **self.env['account.tax']._prepare_base_line_for_taxes_computation(
                line,
                partner_id=commercial_partner,
                currency_id=self.order_id.currency_id,
                rate=self.order_id.currency_rate,
                product_id=line.product_id,
                tax_ids=tax_ids,  # Use filtered taxes
                price_unit=line.price_unit,
                quantity=line.qty * (-1 if is_refund_order else 1),
                discount=line.discount,
                account_id=account_for_tax,
                is_refund=is_refund_line,
                sign=1 if is_refund_order else -1,
            ),
            'uom_id': line.product_uom_id,
            'name': product_name,
        }
