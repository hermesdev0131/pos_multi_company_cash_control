/** @odoo-module */

import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

/**
 * Extend PosOrder to handle multi-company cash control fields.
 *
 * This patch adds support for:
 * - is_fiscal_order: Boolean indicating fiscal/non-fiscal status
 * - non_fiscal_qr_data: Base64 QR code for non-fiscal receipts
 * - order_company_data: Company details for receipt display (stored JSON field)
 */
patch(PosOrder.prototype, {
    /**
     * Override setup to initialize custom fields.
     */
    setup(vals) {
        super.setup(vals);
        // These fields are stored on the order and loaded from backend
        this.is_fiscal_order = vals.is_fiscal_order !== undefined ? vals.is_fiscal_order : true;
        this.non_fiscal_qr_data = vals.non_fiscal_qr_data || false;
        this.order_company_data = vals.order_company_data || false;
    },

    /**
     * Override export_for_printing to include company data and custom fields.
     *
     * This method prepares data for the receipt template.
     * The returned object becomes available as props.data in the template.
     */
    export_for_printing() {
        const result = super.export_for_printing(...arguments);

        // Use order_company_data (stored JSON field) for receipt company info
        if (this.order_company_data && result.headerData) {
            result.headerData.company = this.order_company_data;
        }

        // Add custom fields for receipt logic (QR code display)
        result.is_fiscal_order = this.is_fiscal_order;
        result.non_fiscal_qr_data = this.non_fiscal_qr_data;

        return result;
    },
});
