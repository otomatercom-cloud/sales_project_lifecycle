/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { fmtMoney } from "./dashboard";

export class OtmCustomer360 extends Component {
    static template = "sales_project_lifecycle.Customer360";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        const params = this.props.action && this.props.action.params;
        const ctx = this.props.action && this.props.action.context;
        this.state = useState({
            partnerId: (params && params.partner_id) || (ctx && ctx.default_partner_id) || false,
            data: null, results: [], term: "", error: "", loading: false,
        });
        onWillStart(() => this.load());
    }

    async load() {
        if (!this.state.partnerId) {
            return;
        }
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("otm.dashboard", "get_customer_360", [this.state.partnerId]);
            this.state.error = "";
        } catch (e) {
            this.state.error = (e.data && e.data.message) || String(e.message || e);
        }
        this.state.loading = false;
    }

    money(value) {
        return fmtMoney(value, this.state.data.currency);
    }

    async onSearch(ev) {
        this.state.term = ev.target.value;
        this.state.results = this.state.term.length > 1
            ? await this.orm.call("otm.dashboard", "search_customers", [this.state.term])
            : [];
    }

    async pick(partner) {
        this.state.partnerId = partner.id;
        this.state.results = [];
        this.state.term = "";
        await this.load();
    }

    stars(rating) {
        return "★".repeat(Math.round(rating)) + "☆".repeat(5 - Math.round(rating));
    }
}

registry.category("actions").add("otm_customer360", OtmCustomer360);
