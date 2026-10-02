from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import LifecycleCommon


@tagged('post_install', '-at_install', 'otm_estimate')
class TestEstimate(LifecycleCommon):

    def setUp(self):
        super().setUp()
        self.customer = self.env['res.partner'].create({'name': 'Cust'})
        self.hrms = self.env['otm.service'].create({'name': 'HRMS', 'code': 'TST-HR', 'base_amount': 30000})
        self._lead_to_estimate(self.lead_a1)
        self.est = self.env['otm.estimate'].with_user(self.exec_a1).create({
            'lead_id': self.lead_a1.id, 'customer_id': self.customer.id,
            'line_ids': [(0, 0, {'service_id': self.service.id})]})

    def _add_additional(self, line, amount):
        line.with_user(self.head_a).write({'additional_amount': amount})

    # -- calculations -------------------------------------------------
    def test_base_additional_selling(self):
        line = self.est.line_ids
        self._add_additional(line, 15000)
        self.assertEqual(line.base_unit_price, 20000)
        self.assertEqual(line.selling_unit_price, 35000)
        self.assertAlmostEqual(line.with_user(self.head_a).additional_percentage, 75.0)
        self.assertEqual(self.est.total_amount, 35000)

    def test_multi_service_totals(self):
        self._add_additional(self.est.line_ids, 15000)
        self.est.with_user(self.exec_a1).write({'line_ids': [(0, 0, {'service_id': self.hrms.id})]})
        line2 = self.est.line_ids.filtered(lambda l: l.service_id == self.hrms)
        self._add_additional(line2, 45000)
        self.assertEqual(self.est.base_amount, 50000)
        self.assertEqual(self.est.subtotal, 35000 + 75000)
        self.assertEqual(line2.subtotal, 75000)

    def test_customization_percent_and_fixed(self):
        self._add_additional(self.est.line_ids, 15000)  # subtotal 35000
        C = self.env['otm.estimate.customization'].with_user(self.exec_a1)
        c1 = C.create({'estimate_id': self.est.id, 'customization_type': 'percentage',
                       'percentage': 10, 'description': 'Custom report'})
        c2 = C.create({'estimate_id': self.est.id, 'customization_type': 'fixed',
                       'fixed_amount': 5000, 'description': 'Integration'})
        self.assertEqual(c1.calculated_amount, 3500)
        self.assertEqual(self.est.customization_amount, 8500)
        self.assertEqual(self.est.total_amount, 43500)
        with self.assertRaises(ValidationError):
            C.create({'estimate_id': self.est.id, 'fixed_amount': 1, 'description': ' '})
        self.assertTrue(c2)

    def test_tax_and_recalculate(self):
        self.est.with_user(self.exec_a1).write({'tax_percent': 18})
        self.assertEqual(self.est.tax_amount, 3600)
        self.assertEqual(self.est.total_amount, 23600)
        self.est.with_user(self.exec_a1).action_recalculate()
        self.assertEqual(self.est.total_amount, 23600)

    def test_exec_cannot_touch_additional_amount(self):
        line = self.est.line_ids
        with self.assertRaises(AccessError):
            line.with_user(self.exec_a1).write({'additional_amount': 1000})
        with self.assertRaises(AccessError):
            line.with_user(self.exec_a1).read(['additional_amount'])
        with self.assertRaises(AccessError):
            self.est.with_user(self.exec_a1).read(['additional_amount'])
        self.assertTrue(line.with_user(self.head_a).read(['additional_amount']))

    # -- transitions --------------------------------------------------
    def _to_sent(self):
        self.est.with_user(self.exec_a1).action_submit()
        self.est.with_user(self.head_a).action_send()

    def test_happy_path_and_revision_log(self):
        self._add_additional(self.est.line_ids, 15000)
        self._to_sent()
        self.assertEqual(self.est.status, 'sent')
        self.assertEqual(len(self.est.revision_ids), 1)
        self.est.with_user(self.exec_a1).action_negotiate()
        self.est.with_user(self.head_a).action_approve()
        self.assertEqual(self.est.status, 'approved')
        logs = self.env['otm.transition.log'].search(
            [('res_model', '=', 'otm.estimate'), ('res_id', '=', self.est.id)])
        self.assertEqual(len(logs), 4)

    def test_invalid_transitions(self):
        with self.assertRaises(UserError):
            self.est.with_user(self.head_a).action_send()  # not in review
        with self.assertRaises(UserError):
            self.est.with_user(self.head_a).action_approve()
        self._to_sent()
        with self.assertRaises(UserError):
            self.est.with_user(self.exec_a1).action_submit()
        self.est.with_user(self.head_a).action_approve()
        for m in ('action_negotiate', 'action_send'):
            with self.assertRaises(UserError):
                getattr(self.est.with_user(self.head_a), m)()
        # an approved estimate may be revised only while no deal is locked on it
        self.est.with_user(self.head_a).action_revise(reason='again')
        self.assertEqual(self.est.status, 'draft')

    def test_roles(self):
        self.est.with_user(self.exec_a1).action_submit()
        with self.assertRaises(AccessError):
            self.est.with_user(self.exec_a1).action_send()
        for user in (self.exec_a2, self.head_b, self.exec_b1, self.finance):
            with self.assertRaises(AccessError):
                self.est.with_user(user).action_send()

    def test_direct_write_blocked(self):
        for user in (self.exec_a1, self.head_a, self.admin_user):
            with self.assertRaises(UserError):
                self.est.with_user(user).write({'status': 'approved'})
            with self.assertRaises(UserError):
                self.est.with_user(user).write({'discount_state': 'approved'})
        with self.assertRaises(UserError):
            self.env['otm.estimate'].with_user(self.exec_a1).create({
                'lead_id': self.lead_a1.id, 'status': 'approved'})

    def test_submit_prerequisites(self):
        est = self.env['otm.estimate'].with_user(self.exec_a1).create({'lead_id': self.lead_a1.id})
        with self.assertRaises(UserError) as cm:
            est.action_submit()
        msg = str(cm.exception)
        self.assertIn('customer', msg)
        self.assertIn('no services', msg)

    def test_locking_after_send(self):
        self._to_sent()
        with self.assertRaises(UserError):
            self.est.line_ids.with_user(self.exec_a1).write({'quantity': 5})
        with self.assertRaises(UserError):
            self.est.line_ids.with_user(self.head_a).write({'additional_amount': 1})
        with self.assertRaises(UserError):
            self.est.with_user(self.exec_a1).write({'discount_value': 3})
        with self.assertRaises(UserError):
            self.env['otm.estimate.customization'].with_user(self.exec_a1).create({
                'estimate_id': self.est.id, 'fixed_amount': 1, 'description': 'late'})
        with self.assertRaises(UserError):
            self.est.with_user(self.exec_a1).action_recalculate()

    def test_revision_flow(self):
        self._add_additional(self.est.line_ids, 15000)
        self._to_sent()
        self.est.with_user(self.exec_a1).action_negotiate()
        with self.assertRaises(UserError):
            self.est.with_user(self.exec_a1).action_revise()  # reason mandatory
        self.est.with_user(self.exec_a1).action_revise(reason='Customer wants more')
        self.assertEqual(self.est.status, 'draft')
        self.assertEqual(self.est.revision_number, 2)
        self.assertEqual(self.est.display_name.endswith('-V2'), True)
        self._add_additional(self.est.line_ids, 25000)  # 45000 selling
        self._to_sent()
        revs = self.est.revision_ids.sorted('revision_number')
        self.assertEqual(len(revs), 2)
        self.assertEqual(revs[1].previous_amount, 35000)
        self.assertEqual(revs[1].new_amount, 45000)
        # V3
        self.est.with_user(self.exec_a1).action_reject(reason='Too expensive')
        self.est.with_user(self.exec_a1).action_revise(reason='Lower')
        self.assertEqual(self.est.revision_number, 3)
        # revision history is append-only
        with self.assertRaises(Exception):
            revs[0].with_user(self.admin_user).write({'new_amount': 1})
        with self.assertRaises(Exception):
            revs[0].with_user(self.admin_user).unlink()

    def test_expiry_cron_uses_controlled_transition(self):
        self._to_sent()
        today = fields.Date.context_today(self.est)
        self.est.sudo().write({'estimate_date': today - timedelta(days=5),
                               'validity_date': today - timedelta(days=1)})
        self.env['otm.estimate']._cron_expire_estimates()
        self.assertEqual(self.est.status, 'expired')
        log = self.env['otm.transition.log'].search(
            [('res_model', '=', 'otm.estimate'), ('res_id', '=', self.est.id)], order='id desc', limit=1)
        self.assertEqual(log.to_state, 'Expired')
        self.est.with_user(self.exec_a1).action_revise(reason='Renew offer')
        self.assertEqual(self.est.status, 'draft')

    # -- discount -----------------------------------------------------
    def test_discount_within_exec_limit_needs_no_approval(self):
        self.est.with_user(self.exec_a1).write({'discount_type': 'percentage', 'discount_value': 5})
        self.assertEqual(self.est.discount_required_level, 'none')
        self.assertFalse(self.est.discount_approval_required)
        self.est.with_user(self.exec_a1).action_submit()

    def test_discount_head_level(self):
        self.est.with_user(self.exec_a1).write(
            {'discount_type': 'percentage', 'discount_value': 10, 'discount_reason': 'Loyal'})
        self.assertEqual(self.est.discount_required_level, 'head')
        with self.assertRaises(UserError) as cm:
            self.est.with_user(self.exec_a1).action_submit()
        self.assertIn('needs approval', str(cm.exception))
        self.est.with_user(self.exec_a1).action_request_discount()
        self.assertEqual(self.est.discount_state, 'pending')
        with self.assertRaises(AccessError):
            self.est.with_user(self.exec_a1).action_approve_discount()
        with self.assertRaises(AccessError):
            self.est.with_user(self.head_b).action_approve_discount()
        self.est.with_user(self.head_a).action_approve_discount()
        self.assertEqual(self.est.discount_state, 'approved')
        self.assertEqual(self.est.discount_approved_by_id, self.head_a)
        self.est.with_user(self.exec_a1).action_submit()

    def test_discount_admin_level(self):
        self.est.with_user(self.exec_a1).write(
            {'discount_type': 'percentage', 'discount_value': 25, 'discount_reason': 'Big deal'})
        self.assertEqual(self.est.discount_required_level, 'admin')
        self.est.with_user(self.exec_a1).action_request_discount()
        with self.assertRaises(AccessError):
            self.est.with_user(self.head_a).action_approve_discount()
        self.est.with_user(self.admin_user).action_approve_discount()
        self.assertEqual(self.est.discount_state, 'approved')

    def test_discount_change_resets_approval_and_reject(self):
        self.est.with_user(self.exec_a1).write(
            {'discount_value': 10, 'discount_reason': 'Loyal'})
        self.est.with_user(self.exec_a1).action_request_discount()
        self.est.with_user(self.head_a).action_reject_discount(reason='No')
        self.assertEqual(self.est.discount_state, 'rejected')
        self.est.with_user(self.exec_a1).action_request_discount()
        self.est.with_user(self.head_a).action_approve_discount()
        self.est.with_user(self.exec_a1).write({'discount_value': 12})
        self.assertEqual(self.est.discount_state, 'not_requested')

    def test_head_own_discount_is_authorised_to_own_level(self):
        self.est.with_user(self.head_a).write(
            {'discount_type': 'percentage', 'discount_value': 10, 'discount_reason': 'Head deal'})
        self.assertFalse(self.est.discount_approval_required)
        self.est.with_user(self.head_a).write({'discount_value': 20})
        self.assertTrue(self.est.discount_approval_required)

    def test_discount_validation(self):
        with self.assertRaises(ValidationError):
            self.est.with_user(self.exec_a1).write({'discount_value': 150})
        with self.assertRaises(ValidationError):
            self.est.with_user(self.exec_a1).write({'discount_type': 'fixed', 'discount_value': 99999})
        with self.assertRaises(ValidationError):
            self.est.with_user(self.exec_a1).write({'discount_value': -1})

    # -- security -----------------------------------------------------
    def test_isolation(self):
        E = self.env['otm.estimate']
        self.assertTrue(E.with_user(self.exec_a1).search([('id', '=', self.est.id)]))
        self.assertTrue(E.with_user(self.head_a).search([('id', '=', self.est.id)]))
        for user in (self.exec_a2, self.head_b, self.exec_b1):
            self.assertFalse(E.with_user(user).search([('id', '=', self.est.id)]))
            self.assertFalse(self.env['otm.estimate.line'].with_user(user).search(
                [('estimate_id', '=', self.est.id)]))
        self.assertTrue(E.with_user(self.admin_user).search([('id', '=', self.est.id)]))

    def test_create_estimate_button_and_lead_guard(self):
        lead = self.lead_a2
        with self.assertRaises(UserError):
            lead.with_user(self.exec_a2).action_create_estimate()  # not in estimate stage
        self._lead_to_estimate(lead)
        act = lead.with_user(self.exec_a2).action_create_estimate()
        est = self.env['otm.estimate'].browse(act['res_id'])
        self.assertEqual(est.base_amount, 20000)
        self.assertEqual(lead.estimate_count, 1)
