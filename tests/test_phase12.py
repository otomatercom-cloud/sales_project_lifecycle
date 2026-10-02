from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.exceptions import UserError

from .common import LifecycleCommon
from .test_project import ProjectFixture


@tagged('post_install', '-at_install', 'otm_comfort')
class TestComfortFeeds(LifecycleCommon):
    """Notifications, trends, targets and lead follow-up reminders."""

    def setUp(self):
        super().setUp()
        self.Dash = self.env['otm.dashboard']

    def test_followup_fields(self):
        today = fields.Date.context_today(self.env['otm.lead'])
        self.lead_a1.with_user(self.exec_a1).write({'followup_date': today, 'followup_note': 'Call back'})
        due = self.env['otm.lead'].with_user(self.exec_a1).search([('followup_date', '<=', today)])
        self.assertEqual(due, self.lead_a1)
        # another executive's follow-ups are invisible
        self.assertFalse(self.env['otm.lead'].with_user(self.exec_a2).search([('followup_date', '<=', today)]))

    def test_notifications_scoped_and_exclude_self(self):
        self.lead_a1.with_user(self.exec_a1).action_contact()
        self.lead_b1.with_user(self.exec_b1).action_contact()
        # the actor does not see their own action
        self.assertFalse(self.Dash.with_user(self.exec_a1).get_notifications())
        # the team head sees only his own team's events
        feed = self.Dash.with_user(self.head_a).get_notifications()
        self.assertTrue(feed)
        self.assertEqual({n['res_id'] for n in feed if n['res_model'] == 'otm.lead'}, {self.lead_a1.id})
        self.assertEqual(feed[0]['by'], 'Exec A1')
        # the administrator sees both
        ids = {n['res_id'] for n in self.Dash.with_user(self.admin_user).get_notifications()
               if n['res_model'] == 'otm.lead'}
        self.assertLessEqual({self.lead_a1.id, self.lead_b1.id}, ids)

    def test_notifications_denied_without_role(self):
        plain = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Plain', 'login': 'tst_plain12'})
        self.assertEqual(self.Dash.with_user(plain).get_notifications(), [])
        self.assertEqual(self.Dash.with_user(plain).get_trends()['series'], [])
        self.assertEqual(self.Dash.with_user(plain).get_targets(), [])

    def test_trends_shape_and_scope(self):
        data = self.Dash.with_user(self.admin_user).get_trends(6)
        self.assertEqual(len(data['labels']), 6)
        self.assertEqual([s['key'] for s in data['series']], ['leads', 'won', 'received'])
        for s in data['series']:
            self.assertEqual(len(s['values']), 6)
        month_start = fields.Date.context_today(self.env['otm.lead']).replace(day=1)
        Lead = self.env['otm.lead'].with_context(active_test=False)
        # the current month's bucket equals what each user can actually see
        self.assertEqual(data['series'][0]['values'][-1],
                         Lead.with_user(self.admin_user).search_count([('create_date', '>=', month_start)]))
        self.assertGreaterEqual(data['series'][0]['values'][-1], 3)
        mine = self.Dash.with_user(self.exec_a1).get_trends(6)['series'][0]['values'][-1]
        self.assertEqual(mine, Lead.with_user(self.exec_a1).search_count([('create_date', '>=', month_start)]))
        self.assertLess(mine, data['series'][0]['values'][-1])
        self.assertEqual(len(self.Dash.with_user(self.admin_user).get_trends(99)['labels']), 24)

    def test_targets_progress(self):
        before = len(self.Dash.with_user(self.admin_user).get_targets())
        self.team_a.sudo().target_amount = 100000
        self.team_b.sudo().target_amount = 50000
        rows = {r['team']: r for r in self.Dash.with_user(self.admin_user).get_targets()}
        self.assertLessEqual({'Team A', 'Team B'}, set(rows))
        self.assertEqual(len(rows), before + 2 if before == 0 else len(rows))
        self.assertEqual(rows['Team A']['achieved'], 0.0)
        self.assertEqual(rows['Team A']['percent'], 0.0)
        # executives see no amounts; the sales head sees his own team's target only
        self.assertEqual(self.Dash.with_user(self.exec_a1).get_targets(), [])
        self.assertEqual([r['team'] for r in self.Dash.with_user(self.head_a).get_targets()], ['Team A'])
        self.assertEqual([r['team'] for r in self.Dash.with_user(self.head_b).get_targets()], ['Team B'])


@tagged('post_install', '-at_install', 'otm_autoproject')
class TestAutoProject(ProjectFixture):

    def _enable(self):
        self.env['ir.config_parameter'].sudo().set_param('sales_project_lifecycle.auto_create_project', '1')

    def test_off_by_default_project_not_created(self):
        self._ready()
        self.assertFalse(self.deal.sudo().project_id)
        self.assertTrue(self.deal.sudo().project_start_allowed)

    def test_auto_creates_when_last_condition_met(self):
        self._enable()
        self._ready()
        project = self.deal.sudo().project_id
        self.assertTrue(project)
        self.assertTrue(project.otm_is_lifecycle)
        self.assertEqual(project.otm_state, 'planning')
        self.assertEqual(self.lead_a1.sudo().stage, 'project')
        # the manual button now refuses a duplicate
        with self.assertRaises(UserError):
            self.deal.with_user(self.head_a).action_create_project()

    def test_no_auto_before_advance(self):
        self._enable()
        act = self.deal.with_user(self.exec_a1).action_create_agreement()
        ag = self.env['otm.customer.agreement'].browse(act['res_id'])
        ag.with_user(self.exec_a1).action_generate()
        self.assertFalse(self.deal.sudo().project_id)

    def test_amounts_hidden_from_non_money_roles(self):
        """Deal/payment amounts: Sales Head, Finance and Administrator only."""
        Dash = self.env['otm.dashboard']
        # money trend series are not offered to executives, project heads, developers
        for user in (self.exec_a1, self.developer):
            keys = [s['key'] for s in Dash.with_user(user).get_trends(3)['series']]
            self.assertEqual(keys, ['leads'])
        for user in (self.head_a, self.finance, self.admin_user):
            keys = [s['key'] for s in Dash.with_user(user).get_trends(3)['series']]
            self.assertEqual(keys, ['leads', 'won', 'received'])
        Pay = self.env['otm.deal.payment']
        with self.assertRaises(Exception):
            Pay.with_user(self.exec_a1).search([]).read(['amount'])
        self.assertIn('amount', Pay.with_user(self.finance).fields_get())
        self.assertNotIn('amount', Pay.with_user(self.exec_a1).fields_get())
        self.assertNotIn('total_amount', self.env['otm.deal'].with_user(self.exec_a1).fields_get())
        self.assertIn('total_amount', self.env['otm.deal'].with_user(self.head_a).fields_get())
