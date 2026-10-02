/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

export class OtmProjectBoard extends Component {
    static template = "sales_project_lifecycle.ProjectBoard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ loading: true, columns: [], error: "" });
        onWillStart(() => this.load());
    }

    async load() {
        try {
            const data = await this.orm.call("otm.dashboard", "get_project_board", [{}]);
            this.state.columns = data.columns;
        } catch (e) {
            this.state.error = (e.data && e.data.message) || String(e.message || e);
        }
        this.state.loading = false;
    }

    openProject(card) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "project.project",
            res_id: card.id,
            views: [[false, "form"]],
        });
    }
}

registry.category("actions").add("otm_project_board", OtmProjectBoard);
