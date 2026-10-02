from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import LifecycleCommon


@tagged('post_install', '-at_install', 'otm_demo')
class TestDemo(LifecycleCommon):

    def test_happy_path_and_history(self):
        demo = self._make_demo(self.lead_a1, notes='Showed CRM')
        self.assertEqual(demo.status, 'scheduled')
        demo.with_user(self.exec_a1).action_confirm()
        demo.with_user(self.exec_a1).action_complete()
        self.assertEqual(demo.status, 'completed')
        self.assertTrue(demo.completed_date)
        logs = self.env['otm.transition.log'].search(
            [('res_model', '=', 'otm.demo'), ('res_id', '=', demo.id)])
        self.assertEqual(len(logs), 2)

    def test_invalid_transitions(self):
        demo = self._make_demo(self.lead_a1, notes='x')
        with self.assertRaises(UserError):
            demo.with_user(self.exec_a1).action_complete()  # must be confirmed first
        demo.with_user(self.exec_a1).action_confirm()
        with self.assertRaises(UserError):
            demo.with_user(self.exec_a1).action_confirm()
        demo.with_user(self.exec_a1).action_complete()
        with self.assertRaises(UserError):
            demo.with_user(self.exec_a1).action_cancel(reason='late')

    def test_complete_needs_notes(self):
        demo = self._make_demo(self.lead_a1)
        demo.with_user(self.exec_a1).action_confirm()
        with self.assertRaises(UserError) as cm:
            demo.with_user(self.exec_a1).action_complete()
        self.assertIn('notes', str(cm.exception).lower())

    def test_cancel_and_no_show_need_reason(self):
        d1 = self._make_demo(self.lead_a1)
        with self.assertRaises(UserError):
            d1.with_user(self.exec_a1).action_cancel()
        d1.with_user(self.exec_a1).action_cancel(reason='Customer busy')
        self.assertEqual(d1.status, 'cancelled')
        d2 = self._make_demo(self.lead_a1, start_time=14, end_time=15)
        d2.with_user(self.exec_a1).action_no_show(reason='Did not join')
        self.assertEqual(d2.status, 'no_show')
        self.assertEqual(d2.outcome_reason, 'Did not join')

    def test_direct_status_write_blocked(self):
        demo = self._make_demo(self.lead_a1)
        for user in (self.exec_a1, self.head_a, self.admin_user):
            with self.assertRaises(UserError):
                demo.with_user(user).write({'status': 'completed'})
        with self.assertRaises(UserError):
            self._make_demo(self.lead_a1, status='completed')

    def test_double_booking_blocked(self):
        self._make_demo(self.lead_a1, user=self.exec_a1, start_time=10, end_time=12)
        with self.assertRaises(ValidationError):
            self._make_demo(self.lead_a2, user=self.exec_a2, demo_person_id=self.exec_a1.id,
                            start_time=11, end_time=13)
        # adjacent slot is fine; a cancelled slot frees the person
        other = self._make_demo(self.lead_a2, user=self.exec_a2, demo_person_id=self.exec_a1.id,
                                start_time=12, end_time=13)
        self.assertTrue(other)

    def test_cancelled_demo_frees_slot(self):
        d = self._make_demo(self.lead_a1, start_time=10, end_time=12)
        d.with_user(self.exec_a1).action_cancel(reason='moved')
        self.assertTrue(self._make_demo(self.lead_a1, start_time=10, end_time=12))

    def test_time_and_past_validation(self):
        with self.assertRaises(ValidationError):
            self._make_demo(self.lead_a1, start_time=12, end_time=11)
        with self.assertRaises(ValidationError):
            self._make_demo(self.lead_a1, demo_date=self._future(-2))

    def test_security(self):
        demo = self._make_demo(self.lead_a1, notes='x')
        # other team / other executive cannot see or act
        for user in (self.exec_a2, self.head_b, self.exec_b1):
            self.assertFalse(self.env['otm.demo'].with_user(user).search([('id', '=', demo.id)]))
            with self.assertRaises(AccessError):
                demo.with_user(user).action_confirm()
        # head of the team can act
        demo.with_user(self.head_a).action_confirm()
        self.assertEqual(demo.status, 'confirmed')

    def test_lead_cannot_reach_estimate_without_completed_demo(self):
        lead = self.lead_a1
        for m in ('action_contact', 'action_collect_requirement', 'action_demo'):
            getattr(lead.with_user(self.exec_a1), m)()
        with self.assertRaises(UserError) as cm:
            lead.with_user(self.exec_a1).action_estimate()
        self.assertIn('demo', str(cm.exception).lower())
        self._complete_demo(lead)
        lead.with_user(self.exec_a1).action_estimate()
        self.assertEqual(lead.stage, 'estimate')

    def test_requirement_prerequisite(self):
        lead = self.lead_a1
        lead.sudo().requirement_description = False
        lead.with_user(self.exec_a1).action_contact()
        with self.assertRaises(UserError):
            lead.with_user(self.exec_a1).action_collect_requirement()
