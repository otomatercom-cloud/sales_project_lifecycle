from odoo import _, fields, models
from odoo.exceptions import UserError


class OtmReasonWizard(models.TransientModel):
    _name = 'otm.reason.wizard'
    _description = 'Reason for a Controlled Action'

    res_model = fields.Char(required=True)
    res_id = fields.Integer(required=True)
    method = fields.Char(required=True)
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        record = self.env[self.res_model].browse(self.res_id).exists()
        if not record or self.method not in record._otm_reason_methods:
            raise UserError(_("This action does not accept a reason."))
        if not (self.reason or '').strip():
            raise UserError(_("Please enter a reason."))
        getattr(record, self.method)(reason=self.reason.strip())
        return {'type': 'ir.actions.act_window_close'}
