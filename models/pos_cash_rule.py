# -*- coding: utf-8 -*-

from odoo import models, fields, api


class PosCashCompanyRule(models.Model):
    """
    Model to define rules for routing cash payments to fiscal or non-fiscal companies.
    
    This model will later contain logic to determine which company should receive
    a cash payment based on configurable criteria such as payment method, amount,
    percentage thresholds, and POS configuration.
    """
    _name = 'pos.cash.company.rule'
    _description = 'POS Cash Company Routing Rule'
    _order = 'sequence, id'

    name = fields.Char(
        string='Rule Name',
        required=True,
        help='Descriptive name for this routing rule'
    )
    
    sequence = fields.Integer(
        string='Sequence',
        default=10,
        help='Order in which rules are evaluated'
    )
    
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Uncheck to disable this rule without deleting it'
    )
    
    # Placeholder fields - to be implemented later
    # fiscal_company_id = fields.Many2one(
    #     'res.company',
    #     string='Fiscal Company',
    #     help='Company to use for fiscal cash transactions'
    # )
    # 
    # non_fiscal_company_id = fields.Many2one(
    #     'res.company',
    #     string='Non-Fiscal Company',
    #     help='Company to use for non-fiscal cash transactions'
    # )
    # 
    # target_percentage = fields.Float(
    #     string='Target Percentage',
    #     digits=(16, 2),
    #     help='Percentage of cash payments to route to non-fiscal company'
    # )
    # 
    # cash_payment_method_ids = fields.Many2many(
    #     'pos.payment.method',
    #     string='Cash Payment Methods',
    #     help='Payment methods this rule applies to'
    # )
    # 
    # pos_config_id = fields.Many2one(
    #     'pos.config',
    #     string='POS Configuration',
    #     help='POS configuration this rule applies to'
    # )
