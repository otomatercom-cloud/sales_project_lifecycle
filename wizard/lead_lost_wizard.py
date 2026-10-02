from odoo import fields, models


class OtmLeadLostWizard(models.TransientModel):
    _name = 'otm.lead.lost.wizard'
    _description = 'Mark Lead as Lost'

    lead_id = fields.Many2one('otm.lead', required=True, ondelete='cascade')
    reason = fields.Char(string='Lost Reason', required=True)
    description = fields.Text(string='Description')

    def action_confirm(self):
        self.ensure_one()
        self.lead_id.action_mark_lost(self.reason, self.description)
        return {'type': 'ir.actions.act_window_close'}
