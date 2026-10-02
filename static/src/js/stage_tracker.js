/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class OtmStageTracker extends Component {
    static template = "sales_project_lifecycle.StageTracker";
    static props = { ...standardFieldProps };

    setup() {
        this.state = useState({ open: true });
    }

    get steps() {
        return this.props.record.data[this.props.name] || [];
    }

    get doneCount() {
        return this.steps.filter((s) => s.status === "completed").length;
    }

    get percent() {
        return this.steps.length ? Math.round((this.doneCount * 100) / this.steps.length) : 0;
    }

    get nextStep() {
        return this.steps.find((s) => s.status === "current" || s.status === "blocked");
    }

    toggle() {
        this.state.open = !this.state.open;
    }
}

registry.category("fields").add("otm_stage_tracker", {
    component: OtmStageTracker,
    supportedTypes: ["json"],
});
