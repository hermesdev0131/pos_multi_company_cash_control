# -*- coding: utf-8 -*-

import logging
from odoo import api, models, _
from odoo.exceptions import UserError
from odoo.tools.translate import _lt

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    """
    Override account.move to handle multi-company invoice generation from POS orders.
    
    When POS orders are routed to different companies (fiscal/non-fiscal), invoices
    may need to use journals from the session company even though the invoice belongs
    to the order company. This override bypasses the journal company validation in
    such cases.
    """
    _inherit = 'account.move'

    def _check_company(self, fnames=None):
        """
        Override to bypass journal company validation for multi-company POS invoices.
        
        When invoices are created from POS orders with multi-company routing,
        the journal may belong to the session company while the invoice belongs
        to the order company. This is intentional and should be allowed.
        """
        # Check if we're in a multi-company routing scenario
        # We check both context and invoice origin to detect POS invoices
        bypass_journal_check = False
        is_pos_invoice = (
            self.env.context.get('linked_to_pos') or
            any(move.invoice_origin and 'POS' in str(move.invoice_origin) for move in self) or
            any(move.line_ids.filtered(lambda l: l.move_id and 'POS' in str(l.move_id.name)) for move in self)
        )
        
        if is_pos_invoice:
            # Check if this is a multi-company invoice (invoice company != journal company)
            for move in self:
                if move.journal_id and move.company_id and move.journal_id.company_id.id != move.company_id.id:
                    # Check if order company has no journals (indicates multi-company routing)
                    order_company_journals = self.env['account.journal'].sudo().search([
                        ('company_id', '=', move.company_id.id),
                    ], limit=1)
                    
                    if not order_company_journals:
                        # This is a multi-company routing scenario - skip journal validation
                        bypass_journal_check = True
                        _logger.info(
                            "[POS MCC][INVOICE] _check_company: Bypassing journal company validation for invoice %s. "
                            "Invoice company: %s (id:%d), Journal company: %s (id:%d). "
                            "Order company has no journals configured.",
                            move.name or move.id,
                            move.company_id.name,
                            move.company_id.id,
                            move.journal_id.company_id.name,
                            move.journal_id.company_id.id
                        )
        
        # If we're bypassing journal check, manually validate all other fields
        # We replicate the parent's logic but skip journal_id to prevent parent from resetting fnames
        if bypass_journal_check:
            # Get all fields that need company checking (excluding journal_id)
            if fnames is None:
                # Get all check_company fields except journal_id
                check_fields = [name for name, field in self._fields.items() 
                               if field.relational and field.check_company and name != 'journal_id']
            else:
                # Remove journal_id from provided fnames
                check_fields = [f for f in fnames if f != 'journal_id']
            
            # If no fields left to check, return early (all validation bypassed)
            if not check_fields:
                _logger.debug(
                    "[POS MCC][INVOICE] _check_company: All fields bypassed for POS multi-company invoice(s)"
                )
                return
            
            # Manually validate other fields by replicating parent logic
            # This avoids the parent resetting fnames to include journal_id
            
            regular_fields = []
            property_fields = []
            for name in check_fields:
                field = self._fields[name]
                if field.relational and field.check_company:
                    if not field.company_dependent:
                        regular_fields.append(name)
                    else:
                        property_fields.append(name)
            
            if not (regular_fields or property_fields):
                return
            
            inconsistencies = []
            for record in self:
                # Check regular fields
                if regular_fields:
                    if 'company_id' in self:
                        companies = record.company_id
                    elif 'company_ids' in self:
                        companies = record.company_ids
                    else:
                        continue
                    for name in regular_fields:
                        corecords = record.sudo()[name]
                        if corecords:
                            domain = corecords._check_company_domain(companies)
                            if domain and corecords != corecords.with_context(active_test=False).filtered_domain(domain):
                                inconsistencies.append((record, name, corecords))
                
                # Check property fields
                company = self.env.company
                for name in property_fields:
                    corecords = record.sudo()[name]
                    if corecords:
                        domain = corecords._check_company_domain(company)
                        if domain and corecords != corecords.with_context(active_test=False).filtered_domain(domain):
                            inconsistencies.append((record, name, corecords))
            
            if inconsistencies:
                lines = [_("Incompatible companies on records:")]
                record_msg = _lt("- \"%(record)s\" belongs to company \"%(company)s\" and \"%(field)s\" (%(fname)s: %(values)s) belongs to another company.")
                for record, name, corecords in inconsistencies[:5]:
                    companies = record.company_id if 'company_id' in record else record.company_ids
                    field = self.env['ir.model.fields']._get(self._name, name)
                    lines.append(str(record_msg) % {
                        'record': record.display_name,
                        'company': ", ".join(company.display_name for company in companies),
                        'field': field.field_description,
                        'fname': field.name,
                        'values': ", ".join(repr(rec.display_name) for rec in corecords),
                    })
                raise UserError("\n".join(lines))
            
            # All other fields validated successfully, journal_id was bypassed
            return
        
        # Normal validation for all fields
        return super()._check_company(fnames)

    def _search_default_journal(self):
        """
        Override to return session company journal when order company has no journals.
        
        This handles the multi-company routing scenario where:
        - Invoice belongs to order company (fiscal/non-fiscal)
        - Order company has no journals configured
        - We need to use session company journal
        """
        # Check if we're in a multi-company routing scenario
        if (self.env.context.get('linked_to_pos') and 
            self.env.context.get('pos_mcc_journal_id') and
            self.company_id):
            # Check if order company has no journals
            order_company_journals = self.env['account.journal'].sudo().search([
                ('company_id', '=', self.company_id.id),
            ], limit=1)
            
            if not order_company_journals:
                # Order company has no journals - return session company journal from context
                journal_id = self.env.context.get('pos_mcc_journal_id')
                journal = self.env['account.journal'].browse(journal_id)
                if journal.exists():
                    _logger.info(
                        "[POS MCC][INVOICE] _search_default_journal: Order company '%s' (id:%d) has no journals. "
                        "Returning session company journal '%s' (id:%d).",
                        self.company_id.name,
                        self.company_id.id,
                        journal.name,
                        journal.id
                    )
                    return journal
        
        # Normal behavior - call parent method
        return super()._search_default_journal()

    @api.model_create_multi
    def create(self, vals_list):
        """
        Override create to bypass journal company validation when creating invoices
        from POS orders with multi-company routing.
        
        Strategy: Store journal_id in context, remove from vals, create invoice.
        _search_default_journal() will return the session company journal from context.
        """
        # Check if we're creating invoices from POS with multi-company routing
        bypass_journal_company_check = False
        journal_ids_to_restore = {}
        
        for idx, vals in enumerate(vals_list):
            if self.env.context.get('linked_to_pos') and vals.get('journal_id') and vals.get('company_id'):
                journal = self.env['account.journal'].browse(vals['journal_id'])
                if journal.exists() and journal.company_id.id != vals['company_id']:
                    # This is a POS invoice with journal from different company
                    # Check if order company has no journals (indicates multi-company routing scenario)
                    order_company = self.env['res.company'].browse(vals['company_id'])
                    order_company_journals = self.env['account.journal'].sudo().search([
                        ('company_id', '=', order_company.id),
                    ], limit=1)
                    
                    if not order_company_journals:
                        # Order company has no journals - this is the multi-company routing scenario
                        bypass_journal_company_check = True
                        # Store journal_id in context for _search_default_journal() to use
                        journal_ids_to_restore[idx] = vals.pop('journal_id')
                        _logger.info(
                            "[POS MCC][INVOICE] Temporarily removing journal_id to bypass company validation. "
                            "Invoice company: %s (id:%d), Journal company: %s (id:%d). "
                            "_search_default_journal() will return session company journal.",
                            order_company.name,
                            order_company.id,
                            journal.company_id.name,
                            journal.company_id.id
                        )
        
        # Create invoices with journal_id in context (if bypassing)
        # _search_default_journal() will use the journal from context
        if bypass_journal_company_check and journal_ids_to_restore:
            # Create with context containing journal_id for _search_default_journal()
            # We need to create one at a time to pass the correct journal_id in context
            moves = self.env['account.move']
            for idx, vals in enumerate(vals_list):
                if idx in journal_ids_to_restore:
                    # Create with journal_id in context
                    journal_id = journal_ids_to_restore[idx]
                    move = super(AccountMove, self.with_context(pos_mcc_journal_id=journal_id)).create([vals])
                    moves |= move
                    _logger.info(
                        "[POS MCC][INVOICE] Created invoice %s with journal_id=%d from context.",
                        move.name or move.id,
                        journal_id
                    )
                else:
                    # Normal creation
                    move = super().create([vals])
                    moves |= move
        else:
            # Normal creation path
            moves = super().create(vals_list)
        
        return moves
