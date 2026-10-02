/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const fmtMoney = (value, symbol) => {
    const abs = Math.abs(value);
    let text;
    if (abs >= 10000000) {
        text = (value / 10000000).toFixed(2) + " Cr";
    } else if (abs >= 100000) {
        text = (value / 100000).toFixed(2) + " L";
    } else {
        text = Math.round(value).toLocaleString("en-IN");
    }
    return `${symbol || ""}${text}`;
};

export class OtmDashboard extends Component {
    static template = "sales_project_lifecycle.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ loading: true, data: null, filters: {}, error: "" });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("otm.dashboard", "get_dashboard", [this.state.filters]);
            this.state.error = "";
        } catch (e) {
            this.state.error = (e.data && e.data.message) || String(e.message || e);
        }
        this.state.loading = false;
    }

    formatValue(kpi) {
        return kpi.kind === "sum" ? fmtMoney(kpi.value, this.state.data.currency) : kpi.value.toLocaleString("en-IN");
    }

    money(value) {
        return fmtMoney(value, this.state.data.currency);
    }

    cell(table, row, index) {
        const values = Object.values(row);
        return index === table.money_col ? this.money(values[index]) : values[index];
    }

    barWidth(chart, row) {
        const max = Math.max(...chart.rows.map((r) => r.value), 1);
        return Math.round((row.value / max) * 100);
    }

    async onFilter(ev) {
        const { name, value } = ev.target;
        if (value) {
            this.state.filters[name] = value;
        } else {
            delete this.state.filters[name];
        }
        await this.load();
    }

    async resetFilters() {
        this.state.filters = {};
        await this.load();
    }

    openKpi(kpi) {
        const a = kpi.action;
        this.action.doAction({
            type: "ir.actions.act_window",
            name: a.name,
            res_model: a.res_model,
            domain: a.domain,
            views: [[false, "list"], [false, "form"]],
            context: { active_test: false },
        });
    }
}

registry.category("actions").add("otm_dashboard", OtmDashboard);
export { fmtMoney };
