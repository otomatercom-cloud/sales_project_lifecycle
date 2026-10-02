from datetime import date

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_project import ProjectFixture


@tagged('post_install', '-at_install', 'otm_closure')
class TestClosure(ProjectFixture):

    def setUp(self):
        super().setUp()
        self.deployer = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Deployer', 'login': 'tst_deployer8',
            'group_ids': [(6, 0, [self.env.ref('sales_project_lifecycle.group_developer').id])]})
        self.trainer = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Trainer', 'login': 'tst_trainer8',
            'group_ids': [(6, 0, [self.env.ref('sales_project_lifecycle.group_developer').id])]})
        self.project = self._project()
        self.project.with_user(self.ph).write({
            'user_id': self.ph.id, 'otm_start_date': date(2026, 11, 2),
            'otm_developer_ids': [(6, 0, [self.dev1.id])], 'otm_trainer_id': self.trainer.id,
            'otm_qc_user_id': self.qc.id, 'otm_deploy_user_id': self.deployer.id})
        self.project.with_user(self.ph).action_otm_start()
        self.server = self.env['otm.client.server'].sudo().create({'name': 'S', 'customer_id': self.customer.id})

    def _pay(self, trigger):
        pay = self.deal.payment_ids.filtered(lambda p: p.trigger == trigger)
        pay.with_user(self.fin).write({'payment_reference': 'U' + trigger, 'payment_method': 'bank',
                                       'proof': 'UFJPT0Y=', 'paid_date': fields.Date.today()})
        pay.with_user(self.fin).action_confirm()
        return pay

    def _qc_pass(self):
        t = self._task(self.project)
        t.with_user(self.dev1).action_otm_start()
        t.with_user(self.dev1).write({'otm_progress': 100})
        t.with_user(self.dev1).action_otm_submit()
        t.with_user(self.ph).action_otm_complete()
        act = self.project.with_user(self.ph).action_submit_qc()
        qc = self.env['otm.qc'].browse(act['res_id'])
        qc.with_user(self.qc).action_start()
        qc.with_user(self.qc).action_pass()

    def _deploy_complete(self):
        dep = self.env['otm.deployment'].with_user(self.ph).create({
            'project_id': self.project.id, 'version': '1', 'server_id': self.server.id,
            'backup_confirmed': True})
        dep.with_user(self.ph).action_approve()
        dep.with_user(self.deployer).action_deploy()
        dep.with_user(self.deployer).action_verify()
        dep.with_user(self.deployer).write({'customer_confirmation': True, 'verified_by': 'Cust',
                                            'verification_date': fields.Date.today()})
        dep.with_user(self.deployer).action_complete()

    def _train(self):
        t = self.env['otm.training'].with_user(self.ph).create({
            'project_id': self.project.id, 'training_type': 'online', 'date': date(2026, 12, 1),
            'start_time': 10, 'end_time': 12, 'trainer_id': self.trainer.id})
        t.with_user(self.trainer).write({'topics': 'x', 'participants': 'y', 'customer_confirmation': True})
        t.with_user(self.trainer).action_start()
        t.with_user(self.trainer).action_complete()

    def _ready_for_delivery(self):
        self._qc_pass()
        self._deploy_complete()
        self._train()
        self._pay('after_training')

    def test_delivery_prerequisites(self):
        with self.assertRaises(UserError) as cm:
            self.project.with_user(self.ph).action_final_delivery()
        self.assertIn('customer verification', str(cm.exception))
        self._qc_pass()
        self._deploy_complete()
        self._train_only = self._train()
        with self.assertRaises(UserError) as cm:  # 30% not received
            self.project.with_user(self.ph).action_final_delivery()
        self.assertIn('payment has not been received', str(cm.exception))

    def test_delivery_security_and_due(self):
        self._ready_for_delivery()
        for u in (self.exec_a1, self.head_a, self.dev1, self.fin, self.ph2):
            with self.assertRaises(AccessError, msg=u.name):
                self.project.with_user(u).action_final_delivery()
        final = self.deal.payment_ids.filtered(lambda p: p.trigger == 'final_delivery')
        self.assertEqual(final.status, 'pending')
        self.project.with_user(self.ph).action_final_delivery()
        self.assertEqual(self.project.otm_state, 'delivered')
        self.assertEqual(self.project.otm_delivered_by_id, self.ph)
        self.assertEqual(final.status, 'due')
        self.assertEqual(final.amount, 7000)
        with self.assertRaises(UserError):
            self.project.with_user(self.ph).action_final_delivery()
        with self.assertRaises(UserError):
            self.project.with_user(self.admin_user).write({'otm_state': 'closed'})

    def test_close_blocked_until_final_payment(self):
        self._ready_for_delivery()
        self.project.with_user(self.ph).action_final_delivery()
        with self.assertRaises(UserError) as cm:
            self.project.with_user(self.ph).action_close()
        self.assertIn('has not been received', str(cm.exception))
        self.assertEqual(self.project.otm_state, 'delivered')

    def test_review_and_closure(self):
        self._ready_for_delivery()
        self.project.with_user(self.ph).action_final_delivery()
        Review = self.env['otm.customer.review']
        self.assertFalse(Review.sudo().search([('project_id', '=', self.project.id)]))
        self._pay('final_delivery')
        review = Review.sudo().search([('project_id', '=', self.project.id)])
        self.assertEqual(len(review), 1)
        self.assertEqual(review.status, 'requested')
        with self.assertRaises(UserError):
            Review.with_user(self.head_a).create({'project_id': self.project.id, 'customer_id': self.customer.id})
        # closure does not depend on the customer replying
        for u in (self.exec_a1, self.head_a, self.dev1, self.fin):
            with self.assertRaises(AccessError, msg=u.name):
                self.project.with_user(u).action_close()
        self.project.with_user(self.ph).action_close()
        self.assertEqual(self.project.otm_state, 'closed')
        self.assertEqual(self.project.otm_closed_by_id, self.ph)
        self.assertTrue(self.project.otm_closed_date)
        self.assertEqual(self.lead.stage, 'won')
        self.assertEqual(review.status, 'requested')
        # review submission
        with self.assertRaises(UserError) as cm:
            review.with_user(self.exec_a1).action_submit()
        self.assertIn('required', str(cm.exception))
        for u in (self.exec_a2, self.dev1, self.fin):
            with self.assertRaises(AccessError, msg=u.name):
                review.with_user(u).write({'comments': 'x'})
        review.with_user(self.exec_a1).write({
            'rating': '5', 'service_rating': '4', 'quality_rating': '5', 'support_rating': '5',
            'recommendation': 'yes', 'testimonial_permission': True, 'comments': 'Great'})
        review.with_user(self.exec_a1).action_submit()
        self.assertEqual(review.status, 'submitted')
        self.assertEqual(review.average_rating, 4.75)
        with self.assertRaises(UserError):
            review.with_user(self.exec_a1).write({'comments': 'edit'})
        with self.assertRaises(UserError):
            review.with_user(self.exec_a1).write({'status': 'requested'})
        with self.assertRaises(AccessError):
            review.with_user(self.exec_a1).action_publish()
        review.with_user(self.head_a).action_publish()
        self.assertTrue(review.published)
        with self.assertRaises(UserError):
            review.with_user(self.admin_user).unlink()
        # visibility
        self.assertTrue(Review.with_user(self.head_a).search([('id', '=', review.id)]))
        self.assertFalse(Review.with_user(self.exec_a2).search([('id', '=', review.id)]))
        with self.assertRaises(AccessError):
            Review.with_user(self.dev1).search([('id', '=', review.id)])
        # history
        acts = self.env['otm.transition.log'].sudo().search([('res_id', '=', self.project.id)]).mapped('action')
        self.assertIn('close', acts)
        self.assertIn('deliver', acts)

    def test_commission_on_project_completed(self):
        self._ready_for_delivery()
        self.project.with_user(self.ph).action_final_delivery()
        self._pay('final_delivery')
        self.project.with_user(self.ph).action_close()
        self.assertTrue(self.deal.sudo().commission_ids)

    def test_no_training_required_flow(self):
        self._qc_pass()
        self._deploy_complete()
        self.project.with_user(self.ph).action_final_delivery()
        t30 = self.deal.payment_ids.filtered(lambda p: p.trigger == 'after_training')
        self.assertEqual(t30.status, 'due')
        with self.assertRaises(UserError):
            self.project.with_user(self.ph).action_close()

    def test_closure_creates_client_services(self):
        self._ready_for_delivery()
        self.project.with_user(self.ph).action_final_delivery()
        self._pay('final_delivery')
        self.assertFalse(self.env['otm.client.service'].sudo().search([('deal_id', '=', self.deal.id)]))
        self.project.with_user(self.ph).action_close()
        svcs = self.env['otm.client.service'].sudo().search([('deal_id', '=', self.deal.id)])
        self.assertEqual(len(svcs), len(self.deal.sudo().line_ids))
        self.assertEqual(svcs.mapped('status'), ['active'] * len(svcs))
        self.assertEqual(svcs[0].sales_team_id, self.team_a)
        self.assertEqual(svcs[0].responsible_user_id, self.exec_a1)
