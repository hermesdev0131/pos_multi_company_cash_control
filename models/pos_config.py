# -*- coding: utf-8 -*-

from odoo import models, fields


class PosConfig(models.Model):
    """
    Extension of pos.config model to add multi-company cash control rules.
    
    This adds a One2many relationship to pos.cash.company.rule records,
    allowing rules to be managed directly from the POS configuration form.
    """
    _inherit = 'pos.config'

    cash_company_rule_ids = fields.One2many(
        'pos.cash.company.rule',
        'pos_config_id',
        string='Cash Company Rules',
        help='Rules for routing cash payments to fiscal or non-fiscal companies'
    )
