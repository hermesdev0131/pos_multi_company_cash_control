/** @odoo-module */

import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/store/pos_store";

/**
 * Patch PosStore to ensure computed fields are available in receipt context
 *
 * This ensures that is_fiscal_order and non_fiscal_qr_data are included
 * in the order data when rendering receipts.
 */
console.log('Patch Loaded!');
console.log('PosStore', PosStore);
patch(PosStore.prototype, {
    /**
     * Override getReceiptHeaderData to include our custom computed fields
     */
    getReceiptHeaderData(order) {
        const data = super.getReceiptHeaderData(...arguments);

        // Ensure our computed fields are available in receipt context
        if (order) {
            data.is_fiscal_order = order.is_fiscal_order || false;
            data.non_fiscal_qr_data = order.non_fiscal_qr_data || false;
        }

        return data;
    },
});
