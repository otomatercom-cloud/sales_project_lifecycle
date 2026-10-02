import logging

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

DEFAULT_REMINDER_DAYS = '60,30,15,7,1,0'
PARAM_REMINDER_DAYS = 'sales_project_lifecycle.reminder_days'
PERIOD_MONTHS = {'monthly': 1, 'quarterly': 3, 'half_yearly': 6, 'yearly': 12}


def parse_reminder_days(text):
    """'60, 30,15' -> [60, 30, 15] (descending, unique, >= 0). Raises ValueError on bad input."""
    out = set()
    for token in (text or '').replace(';', ',').split(','):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if value < 0:
            raise ValueError(token)
        out.add(value)
    return sorted(out, reverse=True)


class OtmExpiryReminder(models.Model):
    """One row per reminder actually sent; the unique key prevents duplicates."""
    _name = 'otm.expiry.reminder'
    _description = 'Expiry Reminder Log'
    _order = 'id desc'

    res_model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True, index=True)
    record_name = fields.Char()
    field_name = fields.Char(required=True)
    expiry_date = fields.Date(required=True)
    days_before = fields.Integer(required=True)
    sent_date = fields.Datetime(default=fields.Datetime.now)
    user_id = fields.Many2one('res.users', string='Notified User')

    _uniq = models.Constraint(
        'unique(res_model, res_id, field_name, expiry_date, days_before)',
        'This reminder was already sent.')

    def write(self, vals):
        if not self.env.su:
            from odoo.exceptions import UserError
            raise UserError(_("Reminder history cannot be modified."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            from odoo.exceptions import UserError
            raise UserError(_("Reminder history cannot be deleted."))
        return super().unlink()


class OtmExpiryMixin(models.AbstractModel):
    """Expiry reminders (activities for the responsible user) for dated records."""
    _name = 'otm.expiry.mixin'
    _description = 'Expiry Reminder Mixin'

    def _otm_expiry_specs(self):
        """[(date_field, label)] the cron watches."""
        return []

    def _otm_expiry_domain(self):
        return []

    def _otm_reminder_user(self):
        self.ensure_one()
        return self.env['res.users']

    def _otm_after_expiry_sync(self, today, offsets):
        """Hook: update records' status from dates (service/integration)."""

    @api.model
    def _otm_reminder_offsets(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(PARAM_REMINDER_DAYS) or DEFAULT_REMINDER_DAYS
        try:
            return parse_reminder_days(raw) or parse_reminder_days(DEFAULT_REMINDER_DAYS)
        except ValueError:
            _logger.warning("Invalid reminder days '%s', using defaults.", raw)
            return parse_reminder_days(DEFAULT_REMINDER_DAYS)

    @api.model
    def _cron_expiry_reminders(self, today=None):
        today = today or fields.Date.context_today(self)
        offsets = self._otm_reminder_offsets()
        Log = self.env['otm.expiry.reminder'].sudo()
        sent = 0
        for field_name, label in self._otm_expiry_specs():
            records = self.sudo().search([(field_name, '!=', False)] + self._otm_expiry_domain())
            for rec in records:
                expiry = rec[field_name]
                days_left = (expiry - today).days
                due = [o for o in offsets if o >= days_left]
                if not due:
                    continue
                key = [('res_model', '=', rec._name), ('res_id', '=', rec.id),
                       ('field_name', '=', field_name), ('expiry_date', '=', expiry)]
                done = set(Log.search(key).mapped('days_before'))
                trigger = min(due)
                for o in due:
                    if o not in done:
                        Log.create({'res_model': rec._name, 'res_id': rec.id, 'record_name': rec.display_name,
                                    'field_name': field_name, 'expiry_date': expiry, 'days_before': o,
                                    'user_id': rec._otm_reminder_user().id or False})
                if trigger in done:
                    continue
                rec._otm_send_reminder(label, expiry, days_left)
                sent += 1
        self._otm_sync_status_all(today, offsets)
        return sent

    @api.model
    def _otm_sync_status_all(self, today, offsets):
        self.sudo()._otm_after_expiry_sync(today, offsets)

    def _otm_send_reminder(self, label, expiry, days_left):
        self.ensure_one()
        user = self._otm_reminder_user()
        if days_left > 0:
            when = _("expires in %s day(s)", days_left)
        elif days_left == 0:
            when = _("expires today")
        else:
            when = _("expired %s day(s) ago", -days_left)
        summary = _("%(label)s: %(name)s %(when)s", label=label, name=self.display_name, when=when)
        note = _("%(label)s of %(name)s (%(date)s) %(when)s. Please follow up with the customer for renewal.",
                 label=label, name=self.display_name, date=expiry, when=when)
        if user and hasattr(self, 'activity_schedule'):
            self.activity_schedule('mail.mail_activity_data_todo', date_deadline=fields.Date.context_today(self),
                                   user_id=user.id, summary=summary, note=note)
        self.message_post(body=note, subtype_xmlid='mail.mt_note') if hasattr(self, 'message_post') else None


def add_period(base, months):
    return base + relativedelta(months=months)
