from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import LifecycleCommon


@tagged('post_install', '-at_install', 'otm_security')
class TestSalesTeamSecurity(LifecycleCommon):

    def _visible(self, user, domain=None):
        leads = self.env['otm.lead'].with_user(user).search(domain or [])
        return set(leads.ids) & {self.lead_a1.id, self.lead_a2.id, self.lead_b1.id}

    def test_leads_land_in_creators_team(self):
        self.assertEqual(self.lead_a1.sales_team_id, self.team_a)
        self.assertEqual(self.lead_a1.salesperson_id, self.exec_a1)
        self.assertEqual(self.lead_a1.sales_head_id, self.head_a)
        self.assertEqual(self.lead_b1.sales_team_id, self.team_b)
        self.assertTrue(self.lead_a1.reference.startswith('LD'))

    def test_visibility_matrix(self):
        self.assertEqual(self._visible(self.exec_a1), {self.lead_a1.id})
        self.assertEqual(self._visible(self.exec_a2), {self.lead_a2.id})
        self.assertEqual(self._visible(self.exec_b1), {self.lead_b1.id})
        self.assertEqual(self._visible(self.head_a), {self.lead_a1.id, self.lead_a2.id})
        self.assertEqual(self._visible(self.head_b), {self.lead_b1.id})
        self.assertEqual(
            self._visible(self.admin_user),
            {self.lead_a1.id, self.lead_a2.id, self.lead_b1.id})

    def test_direct_access_to_other_team_denied(self):
        # Direct record access (the equivalent of opening a URL by id).
        for user, lead in ((self.head_a, self.lead_b1), (self.head_b, self.lead_a1),
                           (self.exec_a1, self.lead_a2), (self.exec_a1, self.lead_b1)):
            with self.assertRaises(AccessError, msg=f"{user.name} must not read {lead.name}"):
                lead.with_user(user).read(['name'])
            with self.assertRaises(AccessError):
                lead.with_user(user).write({'name': 'hacked'})

    def test_teams_isolated(self):
        Team = self.env['otm.sales.team']
        self.assertEqual(Team.with_user(self.head_a).search([('code', 'like', 'TST-')]), self.team_a)
        self.assertEqual(Team.with_user(self.head_b).search([('code', 'like', 'TST-')]), self.team_b)
        self.assertEqual(Team.with_user(self.exec_a1).search([('code', 'like', 'TST-')]), self.team_a)
        self.assertEqual(
            Team.with_user(self.admin_user).search([('code', 'like', 'TST-')]),
            self.team_a | self.team_b)
        with self.assertRaises(AccessError):
            self.team_b.with_user(self.head_a).read(['name'])

    def test_executive_cannot_create_for_other_team_or_person(self):
        Lead = self.env['otm.lead'].with_user(self.exec_a1)
        with self.assertRaises(Exception):
            Lead.create({'name': 'x', 'sales_team_id': self.team_b.id})
        with self.assertRaises(Exception):
            Lead.create({'name': 'x', 'salesperson_id': self.exec_a2.id})

    def test_executive_cannot_reassign(self):
        with self.assertRaises(AccessError):
            self.lead_a1.with_user(self.exec_a1).write({'salesperson_id': self.exec_a2.id})
        with self.assertRaises(AccessError):
            self.lead_a1.with_user(self.exec_a1).write({'sales_team_id': self.team_b.id})

    def test_head_reassigns_within_team_only(self):
        self.lead_a1.with_user(self.head_a).write({'salesperson_id': self.exec_a2.id})
        self.assertEqual(self.lead_a1.salesperson_id, self.exec_a2)
        with self.assertRaises(Exception):
            self.lead_a1.with_user(self.head_a).write({'sales_team_id': self.team_b.id})
        # salesperson must belong to the lead's team
        with self.assertRaises(ValidationError):
            self.lead_a1.with_user(self.head_a).write({'salesperson_id': self.exec_b1.id})

    def test_team_membership_change_applies_immediately(self):
        """Rules must follow team edits without a cache clear."""
        self.assertFalse(self.env['otm.lead'].with_user(self.head_a).search(
            [('id', '=', self.lead_b1.id)]))
        self.team_b.write({'head_id': self.head_a.id})
        self.assertIn(self.lead_b1.id, self.env['otm.lead'].with_user(self.head_a).search([]).ids)
        self.assertFalse(self.env['otm.lead'].with_user(self.head_b).search(
            [('id', '=', self.lead_b1.id)]))

    def test_user_computed_roles(self):
        self.assertEqual(self.head_a.otm_sales_role, 'sales_head')
        self.assertEqual(self.exec_a1.otm_sales_role, 'sales_executive')
        self.assertEqual(self.exec_a1.otm_sales_team_ids, self.team_a)
        self.assertEqual(self.head_a.otm_sales_team_ids, self.team_a)
        self.assertEqual(self.admin_user.otm_sales_team_ids, self.env['otm.sales.team'])
        self.exec_a1.otm_primary_sales_team_id = self.team_a
        with self.assertRaises(ValidationError):
            self.exec_a1.otm_primary_sales_team_id = self.team_b

    def test_head_cannot_be_member(self):
        with self.assertRaises(ValidationError):
            self.team_a.member_ids = [(4, self.head_a.id)]

    def test_non_sales_users_see_nothing(self):
        for user in (self.finance, self.developer):
            with self.assertRaises(AccessError):
                self.env['otm.lead'].with_user(user).search([])
            with self.assertRaises(AccessError):
                self.env['otm.sales.team'].with_user(user).search([])

    def test_service_line_isolation(self):
        Line = self.env['otm.lead.service.line']
        line_b = Line.create({'lead_id': self.lead_b1.id, 'service_id': self.service.id})
        line_a = Line.with_user(self.exec_a1).create(
            {'lead_id': self.lead_a1.id, 'service_id': self.service.id})
        ids = {line_a.id, line_b.id}
        self.assertEqual(set(Line.with_user(self.head_a).search([('id', 'in', list(ids))]).ids), {line_a.id})
        self.assertEqual(set(Line.with_user(self.head_b).search([('id', 'in', list(ids))]).ids), {line_b.id})
        self.assertEqual(set(Line.with_user(self.exec_a2).search([('id', 'in', list(ids))]).ids), set())
        with self.assertRaises(AccessError):
            Line.with_user(self.exec_a1).create(
                {'lead_id': self.lead_b1.id, 'service_id': self.service.id})
