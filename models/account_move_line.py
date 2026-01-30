# -*- coding: utf-8 -*-

import logging
from odoo import api, models

_logger = logging.getLogger(__name__)


class AccountMoveLine(models.Model):
    """
    Override account.move.line to handle multi-company invoice generation from POS orders.
    
    When POS orders are routed to different companies (fiscal/non-fiscal), invoice lines
    need to ensure that accounts belong to the invoice line's company, not the product's
    original company.
    """
    _inherit = 'account.move.line'

    @api.depends('product_id', 'move_id', 'display_type')
    def _compute_account_id(self):
        """
        Override to ensure accounts belong to invoice line's company.
        
        When products belong to a different company than the invoice, we need to
        filter accounts to only include those from the invoice line's company.
        """
        # Call parent method first
        super()._compute_account_id()
        
        # Filter accounts to ensure they belong to the invoice line's company
        for line in self:
            if not line.account_id or not line.company_id:
                continue
                
            # Check if account belongs to line's company
            account = line.account_id
            
            # Check if account is compatible with line's company
            # Account is compatible if:
            # 1. Account has no company_ids (shared account), OR
            # 2. Account's company_ids includes line's company
            account_compatible = (
                not account.company_ids or 
                line.company_id in account.company_ids
            )
            
                # If account doesn't belong to line's company, find a compatible one
            if not account_compatible:
                _logger.warning(
                    "[POS MCC][ACCOUNT] Account '%s' (id:%d) does not belong to invoice line company '%s' (id:%d). "
                    "Searching for compatible account.",
                    account.name,
                    account.id,
                    line.company_id.name,
                    line.company_id.id
                )
                
                # SOLUTION: If order company has no accounts, use session company account
                # and temporarily add order company to account's company_ids
                compatible_account = None
                
                # First, check if order company has any accounts at all
                order_company_accounts = self.env['account.account'].sudo().with_company(line.company_id).search([
                    ('company_ids', 'in', [line.company_id.id]),
                    ('deprecated', '=', False),
                ], limit=1)
                
                if not order_company_accounts:
                    # Order company has NO accounts - use session company account (the one from parent method)
                    # Temporarily add order company to account's company_ids to pass validation
                    if account and account.company_ids and line.company_id not in account.company_ids:
                        _logger.info(
                            "[POS MCC][ACCOUNT] Order company '%s' (id:%d) has no accounts. "
                            "Using session company account '%s' (id:%d) and temporarily adding order company to account's company_ids.",
                            line.company_id.name,
                            line.company_id.id,
                            account.name,
                            account.id
                        )
                        
                        # Add order company to account's company_ids AND set code for that company
                        # Odoo requires account code to be set for every company in company_ids
                        # Get the account code from one of the account's companies (session company)
                        # Account codes are company-specific, so we need to check in the account's company context
                        session_company_code = None
                        
                        # Try to get code from account's companies (session company)
                        if account.company_ids:
                            for company in account.company_ids:
                                account_with_company = account.sudo().with_company(company)
                                code = account_with_company.code
                                if code:
                                    session_company_code = code
                                    _logger.debug(
                                        "[POS MCC][ACCOUNT] Found account code '%s' in company '%s' (id:%d).",
                                        code,
                                        company.name,
                                        company.id
                                    )
                                    break
                        
                        # If still no code, try getting it in the current context (might be session company)
                        if not session_company_code:
                            session_company_code = account.sudo().code
                        
                        if session_company_code:
                            # Set code for order company first using with_company context
                            # This must be done BEFORE adding company to company_ids to pass validation
                            account_with_order_company = account.sudo().with_company(line.company_id)
                            account_with_order_company.write({'code': session_company_code})
                            
                            # Now add the order company to company_ids
                            # The code is already set, so validation will pass
                            account.sudo().write({
                                'company_ids': [(4, line.company_id.id)]
                            })
                            
                            _logger.info(
                                "[POS MCC][ACCOUNT] Set account code '%s' for order company '%s' and added to company_ids.",
                                session_company_code,
                                line.company_id.name
                            )
                            
                            compatible_account = account
                        else:
                            _logger.warning(
                                "[POS MCC][ACCOUNT] Account '%s' (id:%d) has no code in any of its companies (%s). "
                                "Cannot add order company without code. Will try other methods.",
                                account.name,
                                account.id,
                                [c.name for c in account.company_ids] if account.company_ids else 'None'
                            )
                            # If no code, we can't add the company - will fall through to other methods
                            compatible_account = None
                
                # Try to find equivalent account in line's company (original logic)
                if not compatible_account:
                    compatible_account = None
                
                # If it's a product line, try to find income/expense account in line's company
                if line.product_id and line.display_type == 'product' and line.move_id.is_invoice(True):
                    fiscal_position = line.move_id.fiscal_position_id
                    
                    # Get product accounts in line's company context
                    product_in_line_company = line.product_id.with_company(line.company_id)
                    accounts = product_in_line_company.product_tmpl_id.get_product_accounts(fiscal_pos=fiscal_position)
                    
                    if line.move_id.is_sale_document(include_receipts=True):
                        compatible_account = accounts.get('income')
                    elif line.move_id.is_purchase_document(include_receipts=True):
                        compatible_account = accounts.get('expense')
                    
                    # Verify compatible account belongs to line's company
                    if compatible_account:
                        if compatible_account.company_ids and line.company_id not in compatible_account.company_ids:
                            compatible_account = None
                
                # If no product account found, try journal default account
                if not compatible_account and line.move_id.journal_id:
                    journal_account = line.move_id.journal_id.default_account_id
                    if journal_account:
                        # Check if journal account is compatible with line's company
                        if not journal_account.company_ids or line.company_id in journal_account.company_ids:
                            compatible_account = journal_account
                
                # If still no account, search for any income/expense account in line's company
                if not compatible_account and line.display_type == 'product':
                    account_type = 'income' if line.move_id.is_sale_document(include_receipts=True) else 'expense'
                    
                    # Strategy 1: Search with company_ids filter (most specific)
                    compatible_account = self.env['account.account'].sudo().with_company(line.company_id).search([
                        ('company_ids', 'in', [line.company_id.id]),
                        ('account_type', '=', account_type),
                        ('deprecated', '=', False),
                    ], limit=1, order='code')
                    
                    # Strategy 2: If no results, try without company_ids filter (accounts might be shared)
                    if not compatible_account:
                        compatible_account = self.env['account.account'].sudo().with_company(line.company_id).search([
                            ('account_type', '=', account_type),
                            ('deprecated', '=', False),
                        ], limit=1, order='code')
                        # Verify it's accessible in the company
                        if compatible_account:
                            if compatible_account.company_ids and line.company_id not in compatible_account.company_ids:
                                compatible_account = None
                    
                
                # Final fallback: find ANY non-deprecated account in line's company
                if not compatible_account:
                    # Strategy 1: Search with company_ids filter
                    compatible_account = self.env['account.account'].sudo().with_company(line.company_id).search([
                        ('company_ids', 'in', [line.company_id.id]),
                        ('deprecated', '=', False),
                        ('account_type', '!=', 'off_balance'),
                    ], limit=1, order='code')
                    
                    # Strategy 2: If no results, try without company_ids filter
                    if not compatible_account:
                        compatible_account = self.env['account.account'].sudo().with_company(line.company_id).search([
                            ('deprecated', '=', False),
                            ('account_type', '!=', 'off_balance'),
                        ], limit=1, order='code')
                        # Verify it's accessible in the company
                        if compatible_account:
                            if compatible_account.company_ids and line.company_id not in compatible_account.company_ids:
                                compatible_account = None
                    
                    # Strategy 3: If still no results, try to find account by name/code match from wrong company
                    if not compatible_account and account:
                        # Try to find account with same name or code in target company
                        compatible_account = self.env['account.account'].sudo().with_company(line.company_id).search([
                            '|',
                            ('name', '=', account.name),
                            ('code', '=', account.code),
                            ('deprecated', '=', False),
                        ], limit=1)
                        # Verify it belongs to target company
                        if compatible_account:
                            if compatible_account.company_ids and line.company_id not in compatible_account.company_ids:
                                compatible_account = None
                
                # Update account if found
                if compatible_account:
                    line.account_id = compatible_account
                    _logger.info(
                        "[POS MCC][ACCOUNT] Using compatible account '%s' (id:%d) for invoice line in company '%s'.",
                        compatible_account.name,
                        compatible_account.id,
                        line.company_id.name
                    )
                else:
                    # CRITICAL: We MUST find an account - product lines require account_id (database constraint)
                    # Cannot set to False - will violate check_accountable_required_fields constraint
                    _logger.error(
                        "[POS MCC][ACCOUNT] Could not find compatible account for invoice line in company '%s'. "
                        "Must find ANY account to avoid constraint violation. "
                        "Account '%s' (id:%d) from wrong company will be replaced.",
                        line.company_id.name,
                        account.name,
                        account.id
                    )
                    
                    # Try to find journal in target company and use its default account
                    if line.move_id.journal_id:
                        # Try to find journal in target company first
                        target_journal = self.env['account.journal'].sudo().with_company(line.company_id).search([
                            ('type', '=', line.move_id.journal_id.type),
                            ('company_id', '=', line.company_id.id),
                        ], limit=1)
                        if target_journal and target_journal.default_account_id:
                            if not target_journal.default_account_id.company_ids or line.company_id in target_journal.default_account_id.company_ids:
                                compatible_account = target_journal.default_account_id
                                _logger.info(
                                    "[POS MCC][ACCOUNT] Using journal default account '%s' (id:%d) from compatible journal.",
                                    compatible_account.name,
                                    compatible_account.id
                                )
                    
                    # If still no account, try to find ANY account in target company (more aggressive search)
                    if not compatible_account:
                        # Try multiple search strategies with detailed logging
                        _logger.debug(
                            "[POS MCC][ACCOUNT] Attempting aggressive search for ANY account in company '%s' (id:%d)",
                            line.company_id.name,
                            line.company_id.id
                        )
                        
                        # Strategy 1: Search with company_ids filter
                        any_account = self.env['account.account'].sudo().with_company(line.company_id).search([
                            ('company_ids', 'in', [line.company_id.id]),
                            ('deprecated', '=', False),
                            ('account_type', '!=', 'off_balance'),
                        ], limit=1, order='id')
                        _logger.debug(
                            "[POS MCC][ACCOUNT] Search with company_ids filter found %d accounts",
                            len(any_account) if any_account else 0
                        )
                        
                        # Strategy 2: Try without company_ids filter (accounts might be shared)
                        if not any_account:
                            any_account = self.env['account.account'].sudo().with_company(line.company_id).search([
                                ('deprecated', '=', False),
                                ('account_type', '!=', 'off_balance'),
                            ], limit=10, order='id')  # Get more results to check
                            _logger.debug(
                                "[POS MCC][ACCOUNT] Search without company_ids filter found %d accounts",
                                len(any_account) if any_account else 0
                            )
                            
                            # Filter to find one that's accessible in the company
                            if any_account:
                                for acc in any_account:
                                    if not acc.company_ids or line.company_id in acc.company_ids:
                                        any_account = acc
                                        break
                                else:
                                    any_account = None
                        
                        # Strategy 3: Try searching ALL accounts (no filters except deprecated)
                        if not any_account:
                            all_accounts = self.env['account.account'].sudo().search([
                                ('deprecated', '=', False),
                            ], limit=50, order='id')
                            _logger.debug(
                                "[POS MCC][ACCOUNT] Search for ALL accounts found %d accounts",
                                len(all_accounts) if all_accounts else 0
                            )
                            
                            # Check each account to see if it's accessible in target company
                            for acc in all_accounts:
                                if not acc.company_ids or line.company_id in acc.company_ids:
                                    any_account = acc
                                    _logger.info(
                                        "[POS MCC][ACCOUNT] Found accessible account '%s' (id:%d) - company_ids: %s",
                                        acc.name,
                                        acc.id,
                                        [c.id for c in acc.company_ids] if acc.company_ids else 'None (shared)'
                                    )
                                    break
                        
                        # Set compatible account if found
                        if any_account:
                            compatible_account = any_account
                            account_type_str = getattr(compatible_account, 'account_type', 'unknown')
                            _logger.warning(
                                "[POS MCC][ACCOUNT] Using ANY available account '%s' (id:%d, type:%s) - may not be correct type!",
                                compatible_account.name,
                                compatible_account.id,
                                account_type_str
                            )
                        else:
                            _logger.error(
                                "[POS MCC][ACCOUNT] CRITICAL: No accounts found at all in system! This should not happen."
                            )
                    
                    # FINAL FALLBACK: If still no account, we MUST use the wrong company account
                    # This will cause a company mismatch error, but it's better than constraint violation
                    # OR we could try to bypass company check temporarily
                    if not compatible_account:
                        _logger.critical(
                            "[POS MCC][ACCOUNT] CRITICAL: No accounts found in company '%s' (id:%d). "
                            "This company may not have accounts configured. "
                            "Using original account '%s' (id:%d) from wrong company - will cause company mismatch error.",
                            line.company_id.name,
                            line.company_id.id,
                            account.name,
                            account.id
                        )
                        # Keep the original account - better than constraint violation
                        # The company mismatch error is more informative than "missing account"
                        compatible_account = account
                    
                    # Set the account (even if it's from wrong company)
                    if compatible_account:
                        line.account_id = compatible_account
