import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OtmTransitionLog(models.Model):
    """Append-only audit trail of controlled state transitions.

    Records are created by the transition engine (with sudo) only. No user
    group has create/write/unlink access, so the history cannot be edited.
    """
    _name = 'otm.transition.log'
    _description = 'State Transition History'
    _order = 'date desc, id desc'

    res_model = fields.Char(string='Model', required=True, index=True)
    res_id = fields.Integer(string='Record ID', required=True, index=True)
    record_name = fields.Char(string='Document')
    action = fields.Char(required=True)
    from_state = fields.Char(string='Previous State')
    to_state = fields.Char(string='New State')
    user_id = fields.Many2one('res.users', string='User', required=True)
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    reason = fields.Text(string='Reason / Comment')
    sales_team_id = fields.Many2one('otm.sales.team', string='Sales Team', index=True)
    owner_id = fields.Many2one('res.users', string='Record Owner', index=True)

    def write(self, vals):
        if not self.env.su:
            raise UserError(_("Transition history cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise UserError(_("Transition history cannot be deleted."))
        return super().unlink()


class OtmTransitionMixin(models.AbstractModel):
    """Reusable pattern: state changes only through `_otm_do_transition`.

    Models declare `_otm_state_field` and `_otm_matrix`:
        {action: {'from': (states...), 'to': 'state', 'system': bool}}
    and may override `_otm_check_actor(action)` (role check) and
    `_otm_prerequisites(action)` (list of missing-condition messages).
    """
    _name = 'otm.transition.mixin'
    _description = 'Controlled State Transition Mixin'

    _otm_state_field = 'stage'
    _otm_matrix = {}

    _otm_reason_methods = ()

    def _otm_state_label(self, value, field_name=None):
        field = self._fields[field_name or self._otm_state_field]
        return dict(field._description_selection(self.env)).get(value, value)

    def _otm_after_transition(self, action, old_state, reason):
        """Hook run after the state was written and logged."""

    def _otm_check_actor(self, action):
        """Raise if the current user may not run `action` on self. Default: allow."""

    def _otm_prerequisites(self, action):
        """Return a list of human readable missing prerequisites for `action`."""
        return []

    def _otm_log_scope(self):
        """Return (sales_team, owner_user) used to scope the audit record rules."""
        return (self.env['otm.sales.team'], self.env['res.users'])

    def _otm_do_transition(self, action, reason=False, extra_vals=None, to_state=None):
        spec = self._otm_matrix.get(action)
        if not spec:
            raise UserError(_("Unknown transition '%s'.", action))
        target = to_state or spec['to']
        field = spec.get('field', self._otm_state_field)
        for rec in self:
            if rec[field] not in spec['from']:
                allowed = ', '.join(self._otm_state_label(s, field) for s in spec['from'])
                raise UserError(_(
                    "%(doc)s cannot be moved to '%(to)s'.\n\n"
                    "Current state: %(current)s\nAllowed from: %(allowed)s",
                    doc=rec.display_name, to=self._otm_state_label(target, field),
                    current=self._otm_state_label(rec[field], field), allowed=allowed))
            if not spec.get('system'):
                rec._otm_check_actor(action)
            missing = rec._otm_prerequisites(action)
            if missing:
                raise UserError(_(
                    "%(doc)s cannot move to '%(to)s'.\n\nMissing prerequisites:\n%(missing)s",
                    doc=rec.display_name, to=self._otm_state_label(target, field),
                    missing='\n'.join(f"- {m}" for m in missing)))
        Log = self.env['otm.transition.log'].sudo()
        for rec in self:
            old = rec[field]
            vals = dict(extra_vals or {})
            vals[field] = target
            rec.with_context(otm_transition=True).write(vals)
            team, owner = rec._otm_log_scope()
            Log.create({
                'res_model': rec._name, 'res_id': rec.id,
                'record_name': rec.display_name, 'action': action,
                'from_state': rec._otm_state_label(old, field),
                'to_state': rec._otm_state_label(target, field),
                'user_id': self.env.user.id, 'reason': reason or False,
                'sales_team_id': team.id, 'owner_id': owner.id,
            })
            rec._otm_after_transition(action, old, reason)
            rec._otm_notify_customer(action)
        return True

    # -- customer e-mails (templates in data/mail_templates.xml) -----------------
    _otm_email_map = {}

    def _otm_email_partner(self):
        """The customer that receives the e-mail (override per model)."""
        return self.env['res.partner']

    def _otm_template_xmlid(self, action):
        return self._otm_email_map.get(action)

    def _otm_notify_customer(self, action):
        xmlid = self._otm_template_xmlid(action)
        if xmlid:
            self._otm_send_mail(xmlid)

    def _otm_send_mail(self, xmlid):
        """Queue a template mail to the customer. Never breaks the business action."""
        template = self.env.ref(xmlid, raise_if_not_found=False)
        sent = False
        for rec in self:
            partner = rec.sudo()._otm_email_partner()
            if not template or not partner.email:
                if hasattr(rec, 'message_post'):
                    rec.sudo().message_post(body=_("Email '%s' not sent: the customer has no email address.",
                                                   template.name if template else xmlid))
                continue
            try:
                with self.env.cr.savepoint():
                    template.sudo().send_mail(rec.id, force_send=False, email_values={
                        'recipient_ids': [(6, 0, partner.ids)], 'email_to': False, 'auto_delete': False})
                sent = True
            except Exception:  # noqa: BLE001 - mail problems must not block the workflow
                _logger.exception("Could not queue template %s for %s", xmlid, rec)
        return sent

    # Generic "ask for a reason" popup used by reject / cancel / revise buttons
    def action_ask_reason(self):
        self.ensure_one()
        method = self.env.context.get('otm_reason_method')
        if method not in self._otm_reason_methods:
            raise UserError(_("This action does not accept a reason."))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env.context.get('otm_reason_title') or _('Reason'),
            'res_model': 'otm.reason.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_res_model': self._name, 'default_res_id': self.id,
                'default_method': method,
            },
        }
