from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape


class OtmMessageTemplate(models.Model):
    """Reusable outreach text. Placeholders: {customer} {company} {reference} {executive} {service}."""
    _name = 'otm.message.template'
    _description = 'Outreach Message Template'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    body = fields.Text(required=True, translate=True,
                       help="Placeholders: {customer} {company} {reference} {executive} {service}")
    channel = fields.Selection([('both', 'WhatsApp & Email'), ('whatsapp', 'WhatsApp'), ('email', 'Email')],
                               default='both', required=True)


class OtmLead(models.Model):
    _inherit = 'otm.lead'

    def action_log_outreach(self, channel, body):
        """Record on the lead that a message was sent (the actual sending happens in the user's own
        WhatsApp / mail app, so nothing leaves Odoo without the user pressing send there)."""
        self.ensure_one()
        if channel not in ('whatsapp', 'email', 'call'):
            raise UserError(_("Unknown channel."))
        label = {'whatsapp': _("WhatsApp"), 'email': _("Email"), 'call': _("Call")}[channel]
        text = Markup('<br/>').join(Markup(html_escape(line)) for line in (body or '').split('\n'))
        self.message_post(
            body=Markup("%s %s:<br/>%s") % (label, _("message prepared by %s", self.env.user.name), text),
            subtype_xmlid='mail.mt_note')
        return True
