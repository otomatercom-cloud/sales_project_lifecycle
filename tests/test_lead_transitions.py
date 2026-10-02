from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import LifecycleCommon


@tagged('post_install', '-at_install', 'otm_security')
class TestLeadTransitions(LifecycleCommon):

    def _walk(self, lead, user, steps):
        for method in steps:
            if method == 'action_estimate':
                self._complete_demo(lead)
            getattr(lead.with_user(user), method)()

    def test_happy_path_and_audit(self):
        lead = self.lead_a1
        self._walk(lead, self.exec_a1, [
            'action_contact', 'action_collect_requirement', 'action_demo',
            'action_estimate', 'action_negotiate'])
        self.assertEqual(lead.stage, 'negotiation')
        logs = self.env['otm.transition.log'].search(
            [('res_model', '=', 'otm.lead'), ('res_id', '=', lead.id)], order='id')
        self.assertEqual(len(logs), 5)
        self.assertEqual(logs[0].from_state, 'New')
        self.assertEqual(logs[0].to_state, 'Contacted')
        self.assertEqual(logs[0].user_id, self.exec_a1)
        self.assertEqual(logs[-1].to_state, 'Negotiation')

    def test_cannot_skip_stages(self):
        with self.assertRaises(UserError) as cm:
            self.lead_a1.with_user(self.exec_a1).action_demo()
        self.assertIn('Allowed from', str(cm.exception))
        self.assertEqual(self.lead_a1.stage, 'new')

    def test_direct_stage_write_blocked_for_everyone(self):
        for user in (self.exec_a1, self.head_a, self.admin_user):
            with self.assertRaises(UserError):
                self.lead_a1.with_user(user).write({'stage': 'won'})
        with self.assertRaises(UserError):
            self.env['otm.lead'].with_user(self.exec_a1).create({'name': 'x', 'stage': 'won'})

    def test_system_transitions_not_public(self):
        lead = self.lead_a1
        self._walk(lead, self.exec_a1, [
            'action_contact', 'action_collect_requirement', 'action_demo',
            'action_estimate', 'action_negotiate'])
        lead._otm_do_transition('system_deal_lock')
        self.assertEqual(lead.stage, 'deal_locked')
        # a system transition must still respect its source states
        with self.assertRaises(UserError):
            lead._otm_do_transition('system_won')

    def test_wrong_role_blocked(self):
        # Head B / Exec A2 cannot even see the lead; sudo-less method call is denied.
        for user in (self.head_b, self.exec_a2, self.finance, self.developer):
            with self.assertRaises(AccessError):
                self.lead_a1.with_user(user).action_contact()
        self.assertEqual(self.lead_a1.stage, 'new')

    def test_head_can_drive_team_lead(self):
        self.lead_a1.with_user(self.head_a).action_contact()
        self.assertEqual(self.lead_a1.stage, 'contacted')
        self.assertEqual(
            self.env['otm.transition.log'].search([('res_id', '=', self.lead_a1.id)]).user_id,
            self.head_a)

    def test_lost_requires_reason_and_records_details(self):
        lead = self.lead_a1.with_user(self.exec_a1)
        with self.assertRaises(UserError):
            lead.action_mark_lost('   ')
        lead.action_mark_lost('Budget too low', 'Customer chose a cheaper vendor')
        self.assertEqual(lead.stage, 'lost')
        self.assertEqual(lead.lost_reason, 'Budget too low')
        self.assertEqual(lead.lost_by_id, self.exec_a1)
        self.assertTrue(lead.lost_date)
        log = self.env['otm.transition.log'].search([('res_id', '=', lead.id)])
        self.assertEqual(log.reason, 'Budget too low')

    def test_lost_description_required_when_configured(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'sales_project_lifecycle.lost_description_required', '1')
        with self.assertRaises(UserError):
            self.lead_a1.with_user(self.exec_a1).action_mark_lost('Budget')
        self.lead_a1.with_user(self.exec_a1).action_mark_lost('Budget', 'details')
        self.assertEqual(self.lead_a1.stage, 'lost')

    def test_lost_wizard(self):
        wiz = self.env['otm.lead.lost.wizard'].with_user(self.exec_a1).create({
            'lead_id': self.lead_a1.id, 'reason': 'No response'})
        wiz.action_confirm()
        self.assertEqual(self.lead_a1.stage, 'lost')

    def test_reopen_only_head_or_admin(self):
        self.lead_a1.with_user(self.exec_a1).action_mark_lost('x')
        with self.assertRaises(AccessError):
            self.lead_a1.with_user(self.exec_a1).action_reopen()
        self.lead_a1.with_user(self.head_a).action_reopen()
        self.assertEqual(self.lead_a1.stage, 'new')
        self.assertFalse(self.lead_a1.lost_reason)
        # the original lost entry is preserved in the history
        self.assertEqual(
            len(self.env['otm.transition.log'].search([('res_id', '=', self.lead_a1.id)])), 2)

    def test_cannot_lose_a_won_lead(self):
        lead = self.lead_a1
        lead.with_context(otm_transition=True).write({'stage': 'project'})
        with self.assertRaises(UserError):
            lead.with_user(self.exec_a1).action_mark_lost('x')

    def test_history_is_read_only(self):
        self.lead_a1.with_user(self.exec_a1).action_contact()
        log = self.env['otm.transition.log'].with_user(self.exec_a1).search(
            [('res_id', '=', self.lead_a1.id)])
        self.assertEqual(len(log), 1)
        for call in (lambda: log.write({'reason': 'edited'}), log.unlink):
            with self.assertRaises(Exception):
                call()
        with self.assertRaises(AccessError):
            self.env['otm.transition.log'].with_user(self.exec_a1).create({
                'res_model': 'otm.lead', 'res_id': 1, 'action': 'x',
                'user_id': self.exec_a1.id})
        # even an administrator cannot edit the trail
        with self.assertRaises(Exception):
            log.with_user(self.admin_user).write({'reason': 'edited'})

    def test_history_visibility_follows_teams(self):
        self.lead_a1.with_user(self.exec_a1).action_contact()
        self.lead_b1.with_user(self.exec_b1).action_contact()
        Log = self.env['otm.transition.log']
        self.assertEqual(Log.with_user(self.head_a).search([]).mapped('res_id'), [self.lead_a1.id])
        self.assertEqual(Log.with_user(self.head_b).search([]).mapped('res_id'), [self.lead_b1.id])
        self.assertEqual(Log.with_user(self.exec_a2).search([]).ids, [])
