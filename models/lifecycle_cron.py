import logging
from datetime import timedelta

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)
PARAM_FOLLOWUP_DAYS = 'sales_project_lifecycle.followup_days'


class OtmLifecycleCron(models.AbstractModel):
    """Daily checks that raise activities for the right people (spec §65-66).

    Every notification is logged in `otm.expiry.reminder`, so running the job any number
    of times a day never creates a second activity for the same event.
    """
    _name = 'otm.lifecycle.cron'
    _description = 'Lifecycle Daily Checks'

    @api.model
    def _followup_days(self):
        try:
            return max(int(self.env['ir.config_parameter'].sudo().get_param(PARAM_FOLLOWUP_DAYS) or 3), 1)
        except ValueError:
            return 3

    @api.model
    def _notify_once(self, record, kind, key_date, user, summary, note):
        """One activity per (record, kind, key_date). Returns True when a new activity was created."""
        record = record.sudo()
        Log = self.env['otm.expiry.reminder'].sudo()
        domain = [('res_model', '=', record._name), ('res_id', '=', record.id),
                  ('field_name', '=', kind), ('expiry_date', '=', key_date)]
        if Log.search_count(domain):
            return False
        if not user:
            return False
        Log.create({'res_model': record._name, 'res_id': record.id, 'record_name': record.display_name,
                    'field_name': kind, 'expiry_date': key_date, 'days_before': 0, 'user_id': user.id})
        if hasattr(record, 'activity_schedule'):
            record.activity_schedule('mail.mail_activity_data_todo', date_deadline=fields.Date.context_today(self),
                                     user_id=user.id, summary=summary, note=note)
        else:
            record.message_notify(partner_ids=user.partner_id.ids, subject=summary, body=note)
        return True

    @api.model
    def _cron_lifecycle_checks(self, today=None):
        today = today or fields.Date.context_today(self)
        days = self._followup_days()
        created = 0
        for check in (self._check_demos, self._check_estimates, self._check_agreements,
                      self._check_payments, self._check_stages, self._check_trainings):
            try:
                with self.env.cr.savepoint():
                    created += check(today, days)
            except Exception:  # noqa: BLE001 - one failing check must not stop the others
                _logger.exception("Lifecycle check %s failed", getattr(check, "__name__", check))
        return created

    @api.model
    def _check_demos(self, today, days):
        n = 0
        demos = self.env['otm.demo'].sudo().search([
            ('status', 'in', ('scheduled', 'confirmed')),
            ('demo_date', 'in', [today, today + timedelta(days=1)])])
        for d in demos:
            when = _("today") if d.demo_date == today else _("tomorrow")
            n += self._notify_once(d, 'demo_reminder', d.demo_date, d.demo_person_id,
                                   _("Demo %(when)s: %(name)s", when=when, name=d.display_name),
                                   _("Demo for %(lead)s is %(when)s.", lead=d.lead_id.display_name, when=when))
        missed = self.env['otm.demo'].sudo().search([
            ('status', 'in', ('scheduled', 'confirmed')), ('demo_date', '<', today)])
        for d in missed:
            n += self._notify_once(d, 'demo_overdue', d.demo_date, d.salesperson_id,
                                   _("Demo not closed: %s", d.display_name),
                                   _("The demo date has passed. Mark it completed, cancelled or no-show."))
        return n

    @api.model
    def _check_estimates(self, today, days):
        n = 0
        limit = today - timedelta(days=days)
        for e in self.env['otm.estimate'].sudo().search([
                ('status', 'in', ('sent', 'negotiation')), ('estimate_date', '<=', limit)]):
            n += self._notify_once(e, 'estimate_followup', e.estimate_date, e.salesperson_id,
                                   _("Follow up estimate %s", e.display_name),
                                   _("The estimate has been waiting for %s days or more.", days))
        return n

    @api.model
    def _check_agreements(self, today, days):
        n = 0
        limit = fields.Datetime.now() - timedelta(days=max(days, 7))
        for a in self.env['otm.customer.agreement'].sudo().search([
                ('status', 'in', ('sent', 'customer_accepted', 'signed')), ('sent_date', '<=', limit)]):
            n += self._notify_once(a, 'agreement_pending', a.sent_date.date(), a.salesperson_id,
                                   _("Agreement pending: %s", a.display_name),
                                   _("The agreement is still not completed. Please follow up with the customer."))
        return n

    @api.model
    def _check_payments(self, today, days):
        n = 0
        limit = today - timedelta(days=days)
        for p in self.env['otm.deal.payment'].sudo().search([
                ('status', 'in', ('due', 'requested')), ('due_date', '<=', limit)]):
            user = p.salesperson_id or p.sales_head_id
            n += self._notify_once(p, 'payment_pending', p.due_date, user,
                                   _("Payment pending: %s", p.name),
                                   _("%(name)s has been due since %(d)s. Please follow up.", name=p.name, d=p.due_date))
        return n

    @api.model
    def _check_stages(self, today, days):
        n = 0
        lines = self.env['otm.project.stage.line'].sudo().search([
            ('state', 'in', ('pending', 'in_progress')), ('planned_deadline', '!=', False),
            ('planned_deadline', '<=', today + timedelta(days=1)), ('project_id.otm_state', '=', 'in_progress')])
        for l in lines:
            project = l.project_id
            if l.planned_deadline < today:
                kind, summary = 'stage_overdue', _("Stage overdue: %(s)s (%(p)s)", s=l.name, p=project.name)
            else:
                kind, summary = 'stage_deadline', _("Stage deadline soon: %(s)s (%(p)s)", s=l.name, p=project.name)
            n += self._notify_once(project, f'{kind}:{l.id}', l.planned_deadline, project.user_id, summary,
                                   _("Planned deadline: %s", l.planned_deadline))
        return n

    @api.model
    def _check_trainings(self, today, days):
        n = 0
        Training = self.env['otm.training'].sudo()
        for t in Training.search([('status', '=', 'scheduled'), ('date', 'in', [today, today + timedelta(days=1)])]):
            n += self._notify_once(t.project_id, f'training_soon:{t.id}', t.date, t.trainer_id,
                                   _("Training soon: %s", t.display_name), _("Scheduled on %s.", t.date))
        for t in Training.search([('status', '=', 'scheduled'), ('date', '<', today)]):
            n += self._notify_once(t.project_id, f'training_overdue:{t.id}', t.date, t.project_id.user_id,
                                   _("Training not held: %s", t.display_name),
                                   _("The session date %s has passed and it is still scheduled.", t.date))
        return n
