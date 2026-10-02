from datetime import date, timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .test_project import ProjectFixture


@tagged('post_install', '-at_install', 'otm_training')
class TestTraining(ProjectFixture):

    def setUp(self):
        super().setUp()
        self.trainer = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Trainer', 'login': 'tst_trainer',
            'group_ids': [(6, 0, [self.env.ref('sales_project_lifecycle.group_developer').id])]})
        self.project = self._project()
        self.project.with_user(self.ph).write({
            'user_id': self.ph.id, 'otm_start_date': date(2026, 11, 2),
            'otm_developer_ids': [(6, 0, [self.dev1.id])], 'otm_trainer_id': self.trainer.id,
            'otm_qc_user_id': self.qc.id, 'otm_deploy_user_id': self.dev1.id})
        self.project.with_user(self.ph).action_otm_start()
        self.deal.invalidate_recordset()
        self.training_pay = self.deal.payment_ids.filtered(lambda p: p.trigger == 'after_training')

    def _deploy(self):
        self.env['otm.deployment'].sudo().create({
            'project_id': self.project.id, 'version': '1', 'status': 'deployed',
            'server_id': self.env['otm.client.server'].sudo().create({
                'name': 'S', 'customer_id': self.customer.id}).id})

    def _session(self, **kw):
        vals = {'project_id': self.project.id, 'training_type': 'online', 'date': date(2026, 12, 1),
                'start_time': 10, 'end_time': 12, 'trainer_id': self.trainer.id}
        vals.update(kw)
        return self.env['otm.training'].with_user(self.ph).create(vals)

    def _complete(self, t):
        t.with_user(self.trainer).write({'topics': 'CRM basics', 'participants': 'A, B',
                                         'customer_confirmation': True})
        t.with_user(self.trainer).action_start()
        t.with_user(self.trainer).action_complete()

    def test_scheduling_rules(self):
        for u in (self.trainer, self.dev1, self.ph2, self.head_a):
            with self.assertRaises(AccessError):
                self.env['otm.training'].with_user(u).create({
                    'project_id': self.project.id, 'date': date(2026, 12, 1), 'trainer_id': self.trainer.id})
        with self.assertRaises(UserError):  # must be the project's trainer
            self._session(trainer_id=self.dev1.id)
        with self.assertRaises(ValidationError):
            self._session(start_time=12, end_time=11)
        self._session()
        with self.assertRaises(ValidationError):  # trainer double-booked
            self._session(start_time=11, end_time=13)
        self.assertTrue(self._session(start_time=12, end_time=13))  # multiple sessions

    def test_workflow_and_prerequisites(self):
        t = self._session()
        with self.assertRaises(UserError) as cm:
            t.with_user(self.trainer).action_start()
        self.assertIn('deployed', str(cm.exception))
        self._deploy()
        for u in (self.dev1, self.exec_a1, self.ph2):
            with self.assertRaises(AccessError):
                t.with_user(u).action_start()
        t.with_user(self.trainer).action_start()
        with self.assertRaises(UserError):
            t.with_user(self.trainer).action_start()
        with self.assertRaises(UserError) as cm:
            t.with_user(self.trainer).action_complete()
        msg = str(cm.exception)
        self.assertIn('topics', msg)
        self.assertIn('not confirmed', msg)
        with self.assertRaises(UserError):
            t.with_user(self.admin_user).write({'status': 'completed'})
        t.with_user(self.trainer).write({'topics': 'x', 'participants': 'y', 'customer_confirmation': True})
        with self.assertRaises(AccessError):
            t.with_user(self.trainer).write({'date': date(2027, 1, 1)})  # trainer: content only
        t.with_user(self.trainer).action_complete()
        self.assertEqual(t.status, 'completed')
        with self.assertRaises(UserError):
            t.with_user(self.ph).write({'notes': 'late'})
        with self.assertRaises(UserError):
            t.with_user(self.ph).action_cancel(reason='x')

    def test_cancel_needs_reason(self):
        t = self._session()
        with self.assertRaises(UserError):
            t.with_user(self.ph).action_cancel()
        t.with_user(self.ph).action_cancel(reason='Customer unavailable')
        self.assertEqual((t.status, t.cancel_reason), ('cancelled', 'Customer unavailable'))
        self.assertTrue(self._session())  # slot freed

    def test_payment_due_after_all_required_training(self):
        self._deploy()
        self.assertEqual(self.training_pay.status, 'pending')
        t1, t2 = self._session(), self._session(start_time=13, end_time=14)
        optional = self._session(start_time=15, end_time=16, required=False)
        self._complete(t1)
        self.assertEqual(self.training_pay.status, 'pending')  # second required session open
        self.assertFalse(self.project.otm_training_done)
        self._complete(t2)
        self.assertTrue(self.project.otm_training_done)
        self.assertEqual(self.training_pay.status, 'due')  # optional session does not block
        self.assertEqual(self.training_pay.due_date, fields.Date.today())
        notes = self.training_pay.message_ids.filtered(lambda m: 'Payment due' in (m.subject or ''))
        self.assertTrue(notes)
        self.assertIn(self.fin.partner_id, notes.partner_ids)
        self.assertEqual(optional.status, 'scheduled')

    def test_cancelling_last_open_required_session_releases_payment(self):
        self._deploy()
        t1, t2 = self._session(), self._session(start_time=13, end_time=14)
        self._complete(t1)
        t2.with_user(self.ph).action_cancel(reason='Not needed')
        self.assertEqual(self.training_pay.status, 'due')

    def test_only_cancelled_sessions_do_not_release_payment(self):
        t = self._session()
        t.with_user(self.ph).action_cancel(reason='x')
        self.assertFalse(self.project.otm_training_done)
        self.assertEqual(self.training_pay.status, 'pending')

    def test_finance_collects_30_percent_with_proof(self):
        self._deploy()
        self._complete(self._session())
        pay = self.training_pay
        self.assertEqual(pay.amount, 10500)
        pay.with_user(self.fin).write({'payment_reference': 'UTR30', 'payment_method': 'upi',
                                       'paid_date': fields.Date.today()})
        with self.assertRaises(UserError) as cm:
            pay.with_user(self.fin).action_confirm()
        self.assertIn('proof', str(cm.exception))
        pay.with_user(self.fin).write({'proof': 'UFJPT0Y=', 'proof_filename': 'proof.pdf'})
        pay.with_user(self.fin).action_confirm()
        self.assertEqual(pay.status, 'received')
        self.assertEqual(self.deal.amount_received, 17500 + 10500)
        with self.assertRaises(UserError):
            pay.with_user(self.fin).write({'proof': False})

    def test_training_visibility(self):
        t = self._session()
        T = self.env['otm.training']
        for u in (self.trainer, self.ph, self.admin_user):
            self.assertTrue(T.with_user(u).search([('id', '=', t.id)]), u.name)
        for u in (self.dev1, self.qc):
            self.assertFalse(T.with_user(u).search([('id', '=', t.id)]), u.name)
        for u in (self.exec_a1, self.head_a, self.fin):
            with self.assertRaises(AccessError):
                T.with_user(u).search([])
        self.project.with_user(self.ph).get_views(
            [(self.env.ref('sales_project_lifecycle.view_otm_project_form').id, 'form')])
        T.with_user(self.trainer).get_views([(False, 'form'), (False, 'list')])
