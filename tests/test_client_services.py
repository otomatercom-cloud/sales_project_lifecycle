from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from ..models.expiry import parse_reminder_days
from .common import LifecycleCommon


@tagged('post_install', '-at_install', 'otm_clients')
class TestClientServices(LifecycleCommon):

    def setUp(self):
        super().setUp()
        self.customer = self.env['res.partner'].create({'name': 'Client X'})
        self.today = fields.Date.context_today(self.env['otm.client.service'])
        self.ph = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'PH', 'login': 'tst_ph9',
            'group_ids': [(6, 0, [self.env.ref('sales_project_lifecycle.group_project_head').id])]})
        self.svc = self._svc(self.exec_a1)

    def _svc(self, user, **kw):
        vals = {'customer_id': self.customer.id, 'service_name': 'Hosting', 'service_kind': 'hosting',
                'amount': 20000, 'billing_type': 'yearly', 'start_date': self.today,
                'expiry_date': self.today + timedelta(days=400), 'renewal_period': 'yearly',
                'renewal_amount': 24000}
        vals.update(kw)
        return self.env['otm.client.service'].with_user(user).create(vals)

    def _cron(self, model='otm.client.service', today=None):
        return self.env[model]._cron_expiry_reminders(today=today)

    def _activities(self, rec):
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', rec._name), ('res_id', '=', rec.id), ('summary', 'not ilike', 'Renewal opportunity')])

    # -- creation / security --------------------------------------------------
    def test_create_and_protection(self):
        self.assertEqual(self.svc.sales_team_id, self.team_a)
        self.assertEqual(self.svc.responsible_user_id, self.exec_a1)
        self.assertEqual(self.svc.status, 'active')
        with self.assertRaises(UserError):
            self.svc.with_user(self.exec_a1).write({'status': 'expired'})
        with self.assertRaises(UserError):
            self._svc(self.exec_a1, status='renewed')
        with self.assertRaises(UserError):
            self.svc.with_user(self.admin_user).unlink()
        with self.assertRaises(AccessError):
            self._svc(self.exec_a1, sales_team_id=self.team_b.id)
        with self.assertRaises(AccessError):
            self.svc.with_user(self.exec_a1).write({'sales_team_id': self.team_b.id})
        with self.assertRaises(AccessError):
            self.svc.with_user(self.finance).write({'notes': 'x'})
        with self.assertRaises(ValidationError):
            self._svc(self.exec_a1, expiry_date=False)
        with self.assertRaises(ValidationError):
            self._svc(self.exec_a1, expiry_date=self.today - timedelta(days=5))

    def test_visibility(self):
        S = self.env['otm.client.service']
        dom = [('id', '=', self.svc.id)]
        for u in (self.exec_a1, self.head_a, self.finance, self.ph, self.admin_user):
            self.assertTrue(S.with_user(u).search(dom), u.name)
        for u in (self.exec_a2, self.exec_b1, self.head_b):
            self.assertFalse(S.with_user(u).search(dom), u.name)
        with self.assertRaises(AccessError):
            S.with_user(self.developer).search(dom)

    def test_cancel(self):
        with self.assertRaises(UserError):
            self.svc.with_user(self.head_a).action_cancel(reason=' ')
        for u in (self.exec_a1, self.finance):
            with self.assertRaises(AccessError):
                self.svc.with_user(u).action_cancel(reason='x')
        self.svc.with_user(self.head_a).action_cancel(reason='Customer left')
        self.assertEqual(self.svc.status, 'cancelled')
        with self.assertRaises(UserError):
            self.svc.with_user(self.head_a).action_cancel(reason='again')
        with self.assertRaises(UserError):
            self.svc.with_user(self.head_a).write({'notes': 'x'})
        log = self.env['otm.transition.log'].sudo().search([('res_model', '=', 'otm.client.service'),
                                                            ('res_id', '=', self.svc.id)])
        self.assertEqual(log.action, 'cancel')

    # -- reminders -------------------------------------------------------------
    def test_parse_and_setting_validation(self):
        self.assertEqual(parse_reminder_days('7, 30;60,30,0'), [60, 30, 7, 0])
        with self.assertRaises(ValueError):
            parse_reminder_days('7,abc')
        with self.assertRaises(ValueError):
            parse_reminder_days('-1')
        with self.assertRaises(ValidationError):
            self.env['res.config.settings'].create({'otm_reminder_days': '7,x'})
        self.env['res.config.settings'].create({'otm_reminder_days': '10,5'})

    def test_reminders_no_duplicates_and_status(self):
        svc = self._svc(self.exec_a1, expiry_date=self.today + timedelta(days=60))
        self._cron()
        self.assertEqual(len(self._activities(svc)), 1)
        self.assertEqual(self._activities(svc).user_id, self.exec_a1)
        self.assertEqual(svc.status, 'expiring')
        self._cron()
        self._cron()
        self.assertEqual(len(self._activities(svc)), 1)  # no duplicates
        svc.sudo().write({'expiry_date': self.today + timedelta(days=30)})
        self._cron()
        self.assertEqual(len(self._activities(svc)), 2)
        self._cron()
        self.assertEqual(len(self._activities(svc)), 2)
        # far-future service is untouched
        self.assertEqual(len(self._activities(self.svc)), 0)
        self.assertEqual(self.svc.status, 'active')

    def test_cron_catches_up_after_missed_days(self):
        svc = self._svc(self.exec_a1, expiry_date=self.today + timedelta(days=3))
        self._cron()
        self.assertEqual(len(self._activities(svc)), 1)  # one reminder (7d window), not 4
        self.assertEqual(len(self.env['otm.expiry.reminder'].sudo().search([('res_id', '=', svc.id)])), 4)

    def test_expiry_and_on_expiry_reminder(self):
        svc = self._svc(self.exec_a1, start_date=self.today - timedelta(days=100),
                        expiry_date=self.today + timedelta(days=1))
        self._cron()
        self.assertEqual(svc.status, 'expiring')
        self._cron(today=self.today + timedelta(days=1))  # on expiry
        self.assertEqual(len(self._activities(svc)), 2)
        self.assertEqual(svc.status, 'expiring')
        self._cron(today=self.today + timedelta(days=2))  # after expiry
        self.assertEqual(svc.status, 'expired')

    def test_configurable_days(self):
        self.env['ir.config_parameter'].sudo().set_param('sales_project_lifecycle.reminder_days', '10,0')
        svc = self._svc(self.exec_a1, expiry_date=self.today + timedelta(days=45))
        self._cron()
        self.assertFalse(self._activities(svc))
        self.assertEqual(svc.status, 'active')  # outside the 10 day window
        svc.sudo().write({'expiry_date': self.today + timedelta(days=9)})
        self._cron()
        self.assertEqual(len(self._activities(svc)), 1)
        self.assertEqual(svc.status, 'expiring')

    def test_cancelled_service_gets_no_reminder(self):
        svc = self._svc(self.exec_a1, expiry_date=self.today + timedelta(days=5))
        svc.with_user(self.head_a).action_cancel(reason='x')
        self._cron()
        self.assertFalse(self._activities(svc))

    def test_server_reminders(self):
        server = self.env['otm.client.server'].with_user(self.ph).create({
            'name': 'S1', 'customer_id': self.customer.id, 'responsible_id': self.developer.id,
            'hosting_expiry_date': self.today + timedelta(days=7), 'ssl_expiry_date': self.today + timedelta(days=15)})
        self.assertEqual(self._cron('otm.client.server'), 2)
        self.assertEqual(len(self._activities(server)), 2)
        self.assertEqual(self._activities(server).mapped('user_id'), self.developer)
        self._cron('otm.client.server')
        self.assertEqual(len(self._activities(server)), 2)

    # -- renewal ---------------------------------------------------------------
    def _renewal(self, svc=None):
        svc = svc or self.svc
        act = svc.with_user(self.exec_a1).action_start_renewal()
        return self.env['otm.service.renewal'].browse(act['res_id'])

    def test_renewal_full_flow(self):
        old = self.svc.expiry_date
        r = self._renewal()
        self.assertEqual(r.amount, 24000)
        self.assertEqual(r.new_expiry_date, old + relativedelta(years=1))
        with self.assertRaises(UserError):  # one open renewal at a time
            self.svc.with_user(self.exec_a1).action_start_renewal()
        with self.assertRaises(UserError):  # cannot skip the workflow
            r.with_user(self.exec_a1).action_renew()
        with self.assertRaises(UserError):
            r.with_user(self.exec_a1).write({'status': 'paid'})
        with self.assertRaises(AccessError):
            r.with_user(self.exec_a2).action_send_estimate()
        r.with_user(self.exec_a1).write({'amount': 25000})
        r.with_user(self.exec_a1).action_send_estimate()
        with self.assertRaises(UserError) as cm:
            r.with_user(self.exec_a1).action_approve()
        self.assertIn('approval reference', str(cm.exception))
        r.with_user(self.exec_a1).write({'approval_reference': 'PO-77'})
        r.with_user(self.exec_a1).action_approve()
        with self.assertRaises(UserError):
            r.with_user(self.exec_a1).write({'amount': 1})  # locked after approval
        for u in (self.exec_a1, self.head_a):
            with self.assertRaises(AccessError):
                r.with_user(u).action_confirm_payment()
            with self.assertRaises(AccessError):
                r.with_user(u).write({'payment_reference': 'X'})
        r.with_user(self.finance).write({'payment_reference': 'UTR1', 'payment_method': 'bank',
                                         'paid_date': self.today - timedelta(days=1)})
        with self.assertRaises(UserError) as cm:
            r.with_user(self.finance).action_confirm_payment()
        self.assertIn('proof', str(cm.exception))
        r.with_user(self.finance).write({'proof': 'UFJPT0Y=', 'proof_filename': 'p.pdf'})
        r.with_user(self.finance).action_confirm_payment()
        self.assertEqual(r.status, 'paid')
        self.assertEqual(self.svc.expiry_date, old)  # not renewed until the Renew step
        with self.assertRaises(AccessError):
            r.with_user(self.exec_a2).action_renew()
        r.with_user(self.head_a).action_renew()
        self.assertEqual(r.status, 'completed')
        self.assertEqual(self.svc.status, 'renewed')
        self.assertEqual(self.svc.expiry_date, old + relativedelta(years=1))
        self.assertEqual(self.svc.amount, 25000)
        self.assertLessEqual(abs((self.svc.last_renewed_date - self.today).days), 1)  # user time zone
        with self.assertRaises(UserError):
            r.with_user(self.head_a).action_cancel(reason='late')
        # a new cycle can start, and reminders restart for the new expiry
        self.assertTrue(self._renewal())

    def test_renewal_of_expired_service_counts_from_today(self):
        svc = self._svc(self.exec_a1, start_date=self.today - timedelta(days=500),
                        expiry_date=self.today - timedelta(days=10), renewal_period='quarterly')
        svc.sudo()._otm_do_transition('system_expire')
        r = self._renewal(svc)
        self.assertEqual(r.new_expiry_date, self.today + relativedelta(months=3))

    def test_renewal_reminder_restarts_after_renewal(self):
        svc = self._svc(self.exec_a1, expiry_date=self.today + timedelta(days=7), renewal_period='monthly')
        self._cron()
        self.assertEqual(len(self._activities(svc)), 1)
        r = self.env['otm.service.renewal'].sudo().search([('service_id', '=', svc.id)])
        self.assertEqual(len(r), 1)  # opened automatically when the service started expiring
        r.with_user(self.exec_a1).action_send_estimate()
        r.with_user(self.exec_a1).write({'approval_reference': 'OK'})
        r.with_user(self.exec_a1).action_approve()
        r.with_user(self.finance).write({'payment_reference': 'U', 'payment_method': 'upi',
                                         'paid_date': self.today - timedelta(days=1), 'proof': 'UFJPT0Y='})
        r.with_user(self.finance).action_confirm_payment()
        r.with_user(self.head_a).action_renew()
        self.assertEqual(svc.status, 'renewed')
        self._cron()
        # the renewed expiry (~37 days away) is inside the 60 day window: a fresh cycle, one new reminder
        self.assertEqual(len(self._activities(svc)), 2)
        self._cron()
        self.assertEqual(len(self._activities(svc)), 2)
        self.assertEqual(svc.status, 'expiring' if (svc.expiry_date - self.today).days <= 60 else 'renewed')

    def test_renewal_cancel_and_service_cancel_cascade(self):
        r = self._renewal()
        with self.assertRaises(UserError):
            r.with_user(self.exec_a1).action_cancel(reason='')
        self.svc.with_user(self.head_a).action_cancel(reason='closing')
        self.assertEqual(r.status, 'cancelled')
        with self.assertRaises(UserError):
            self.svc.with_user(self.exec_a1).action_start_renewal()

    def test_one_time_service_cannot_renew(self):
        svc = self._svc(self.exec_a1, billing_type='one_time', renewal_period=False, expiry_date=False)
        with self.assertRaises(UserError):
            svc.with_user(self.exec_a1).action_start_renewal()

    def test_renewal_visibility(self):
        r = self._renewal()
        R = self.env['otm.service.renewal']
        for u in (self.exec_a1, self.head_a, self.finance, self.ph, self.admin_user):
            self.assertTrue(R.with_user(u).search([('id', '=', r.id)]), u.name)
        for u in (self.exec_a2, self.exec_b1, self.head_b):
            self.assertFalse(R.with_user(u).search([('id', '=', r.id)]), u.name)
        with self.assertRaises(UserError):
            R.with_user(self.admin_user).create({'service_id': self.svc.id})

    # -- integrations ----------------------------------------------------------
    def _integration(self, user=None, **kw):
        vals = {'name': 'WhatsApp API', 'customer_id': self.customer.id, 'integration_type': 'whatsapp',
                'provider': 'Meta', 'setup_amount': 5000, 'recurring_amount': 1000,
                'expiry_date': self.today + timedelta(days=30), 'sales_team_id': self.team_a.id,
                'responsible_developer_id': self.developer.id}
        vals.update(kw)
        return self.env['otm.client.integration'].with_user(user or self.head_a).create(vals)

    def test_integration_lifecycle(self):
        for u in (self.exec_a1, self.developer, self.finance):
            with self.assertRaises(AccessError):
                self._integration(u)
        i = self._integration()
        self.assertEqual(i.setup_amount, 5000)
        self.assertEqual(i.recurring_amount, 1000)
        with self.assertRaises(UserError):
            i.with_user(self.head_a).write({'status': 'expired'})
        with self.assertRaises(UserError) as cm:
            i.with_user(self.head_a).action_renew()
        self.assertIn('renewal date', str(cm.exception))
        i.with_user(self.head_a).write({'renewal_date': self.today + timedelta(days=10)})
        with self.assertRaises(UserError):
            i.with_user(self.head_a).action_renew()  # not after current expiry
        new = self.today + timedelta(days=395)
        i.with_user(self.head_a).write({'renewal_date': new})
        with self.assertRaises(AccessError):
            i.with_user(self.head_b).action_renew()
        i.with_user(self.head_a).action_renew()
        self.assertEqual(i.expiry_date, new)
        self.assertFalse(i.renewal_date)
        self.assertEqual(i.status, 'active')
        i.with_user(self.head_a).action_cancel(reason='stopped')
        self.assertEqual(i.status, 'cancelled')

    def test_integration_reminders_and_visibility(self):
        i = self._integration(expiry_date=self.today + timedelta(days=15))
        self._cron('otm.client.integration')
        self.assertEqual(i.status, 'expiring')
        acts = self._activities(i)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts.user_id, self.developer)
        self._cron('otm.client.integration')
        self.assertEqual(len(self._activities(i)), 1)
        I = self.env['otm.client.integration']
        for u in (self.developer, self.head_a, self.ph, self.admin_user):
            self.assertTrue(I.with_user(u).search([('id', '=', i.id)]), u.name)
        self.assertFalse(I.with_user(self.head_b).search([('id', '=', i.id)]))
        with self.assertRaises(AccessError):
            I.with_user(self.exec_b1).search([('id', '=', i.id)])
        with self.assertRaises(AccessError):
            I.with_user(self.exec_a1).search([('id', '=', i.id)])
