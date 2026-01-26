# -*- coding: utf-8 -*-

from odoo import api, models
import logging

_logger = logging.getLogger(__name__)


class ResCompany(models.Model):
    """
    Extension of res.company to ensure company data is loaded for POS orders.
    
    This ensures that when POS loads order data, it also loads the company records
    for all companies used by those orders, so the receipt template can access
    company information (name, logo, address, etc.).
    """
    _inherit = 'res.company'

    @api.model
    def _load_pos_data_domain(self, data):
        """
        Override to include company IDs from orders when loading company data.
        
        CRITICAL: This ensures that when POS loads orders with different company_ids
        than the session company, those company records are also loaded so the
        receipt template can display the correct company information.
        
        Args:
            data (dict): POS data dictionary containing loaded models
            
        Returns:
            list: Domain filter for loading company records
        """
        # Get the default company from config (session company)
        company_ids = []
        
        try:
            # Get session company from POS config
            if 'pos.config' in data and data['pos.config'].get('data'):
                config_data = data['pos.config']['data']
                if config_data and isinstance(config_data, list) and len(config_data) > 0:
                    config_company_id = config_data[0].get('company_id')
                    if config_company_id:
                        # company_id is a Many2one field, so it's [id, name] or just id
                        if isinstance(config_company_id, (list, tuple)) and len(config_company_id) > 0:
                            company_ids.append(config_company_id[0])
                        elif isinstance(config_company_id, int):
                            company_ids.append(config_company_id)
            
            # Also include company_ids from orders if they exist
            if 'pos.order' in data and data['pos.order'].get('data'):
                order_data_list = data['pos.order']['data']
                if isinstance(order_data_list, list):
                    for order in order_data_list:
                        if isinstance(order, dict):
                            order_company_id = order.get('company_id')
                            if order_company_id:
                                # company_id is a Many2one field, so it's [id, name] or just id
                                if isinstance(order_company_id, (list, tuple)) and len(order_company_id) > 0:
                                    company_id = order_company_id[0]
                                    if company_id and company_id not in company_ids:
                                        company_ids.append(company_id)
                                elif isinstance(order_company_id, int):
                                    if order_company_id not in company_ids:
                                        company_ids.append(order_company_id)
            
            # Remove duplicates and ensure we have valid IDs
            company_ids = [cid for cid in company_ids if cid]
            
            if company_ids:
                _logger.debug(
                    "[POS MCC][COMPANY] Loading companies for POS: %s",
                    company_ids
                )
                # Return domain to load all companies used by orders
                if len(company_ids) == 1:
                    return [('id', '=', company_ids[0])]
                else:
                    return [('id', 'in', company_ids)]
        except Exception as e:
            _logger.error(
                "[POS MCC][COMPANY] Error in _load_pos_data_domain: %s",
                str(e)
            )
        
        # Fallback to parent method
        return super()._load_pos_data_domain(data) if hasattr(super(), '_load_pos_data_domain') else []
