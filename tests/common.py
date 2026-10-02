from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class LifecycleCommon(TransactionCase):
    """Fixture: Team A (Head A, A1, A2), Team B (Head B, B1), Admin, Finance, Developer."""

    def setUp(self):
        super().setUp()
        Users = self.env['res.users'].with_context(no_reset_password=True)
        ref = self.env.ref

        def make(name, login, xmlid):
            return Users.create({
                'name': name, 'login': login,
                'group_ids': [(6, 0, [ref(xmlid).id])],
            })

        self.head_a = make('Head A', 'tst_head_a', 'sales_project_lifecycle.group_sales_head')
        self.exec_a1 = make('Exec A1', 'tst_exec_a1', 'sales_project_lifecycle.group_sales_executive')
        self.exec_a2 = make('Exec A2', 'tst_exec_a2', 'sales_project_lifecycle.group_sales_executive')
        self.head_b = make('Head B', 'tst_head_b', 'sales_project_lifecycle.group_sales_head')
        self.exec_b1 = make('Exec B1', 'tst_exec_b1', 'sales_project_lifecycle.group_sales_executive')
        self.admin_user = make('Lifecycle Admin', 'tst_admin', 'sales_project_lifecycle.group_lifecycle_admin')
        self.finance = make('Finance', 'tst_fin', 'sales_project_lifecycle.group_finance')
        self.developer = make('Developer A', 'tst_dev', 'sales_project_lifecycle.group_developer')

        Team = self.env['otm.sales.team']
        self.team_a = Team.create({
            'name': 'Team A', 'code': 'TST-A', 'head_id': self.head_a.id,
            'member_ids': [(6, 0, [self.exec_a1.id, self.exec_a2.id])]})
        self.team_b = Team.create({
            'name': 'Team B', 'code': 'TST-B', 'head_id': self.head_b.id,
            'member_ids': [(6, 0, [self.exec_b1.id])]})

        self.service = self.env['otm.service'].create({
            'name': 'Test CRM', 'code': 'TST-CRM', 'base_amount': 20000})

        Lead = self.env['otm.lead']
        self.lead_a1 = Lead.with_user(self.exec_a1).create({'name': 'Lead A1'})
        self.lead_a2 = Lead.with_user(self.exec_a2).create({'name': 'Lead A2'})
        self.lead_b1 = Lead.with_user(self.exec_b1).create({'name': 'Lead B1'})
        for lead in (self.lead_a1, self.lead_a2, self.lead_b1):
            lead.sudo().write({
                'requirement_description': 'Need CRM',
                'service_line_ids': [(0, 0, {'service_id': self.service.id, 'quantity': 1})]})

    def _future(self, days=1):
        return fields.Date.context_today(self.env['otm.demo']) + timedelta(days=days)

    def _make_demo(self, lead, user=None, **kw):
        vals = {'lead_id': lead.id, 'demo_date': self._future(), 'start_time': 10, 'end_time': 11,
                'demo_person_id': (user or lead.salesperson_id).id}
        vals.update(kw)
        return self.env['otm.demo'].with_user(user or lead.salesperson_id).create(vals)

    def _complete_demo(self, lead):
        """Run a full valid demo cycle so the lead can move to the estimate stage."""
        user = lead.salesperson_id
        demo = self._make_demo(lead, start_time=1, end_time=2, notes='done',
                               demo_date=self._future(30 + lead.id % 300))
        demo.with_user(user).action_confirm()
        demo.with_user(user).action_complete()
        return demo

    def _lead_to_estimate(self, lead):
        user = lead.salesperson_id
        lead.with_user(user).action_contact()
        lead.with_user(user).action_collect_requirement()
        lead.with_user(user).action_demo()
        self._complete_demo(lead)
        lead.with_user(user).action_estimate()
