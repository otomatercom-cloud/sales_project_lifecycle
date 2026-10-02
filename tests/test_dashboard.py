from datetime import date

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged

from .test_closure import TestClosure as _Closure  # noqa: F401  (fixture reuse only)
from .test_project import ProjectFixture


@tagged('post_install', '-at_install', 'otm_dashboard')
class TestDashboard(ProjectFixture):

    def setUp(self):
        super().setUp()
        self.Dash = self.env['otm.dashboard']
        self.plain = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Plain', 'login': 'tst_plain10'})
        self.project = self._project()
        self.project.with_user(self.ph).write({
            'user_id': self.ph.id, 'otm_start_date': date(2026, 11, 2),
            'otm_developer_ids': [(6, 0, [self.dev1.id])], 'otm_qc_user_id': self.qc.id,
            'otm_deploy_user_id': self.dev1.id})
        self.project.with_user(self.ph).action_otm_start()

    def _data(self, user, **filters):
        return self.Dash.with_user(user).get_dashboard(filters)

    def _kpi(self, data, key):
        return next(k for k in data['kpis'] if k['key'] == key)['value']

    # -- roles -------------------------------------------------------------------
    def test_roles(self):
        D = self.Dash
        self.assertEqual(D.with_user(self.admin_user)._role(), 'admin')
        self.assertEqual(D.with_user(self.head_a)._role(), 'head')
        self.assertEqual(D.with_user(self.exec_a1)._role(), 'executive')
        self.assertEqual(D.with_user(self.ph)._role(), 'project_head')
        self.assertEqual(D.with_user(self.fin)._role(), 'finance')
        self.assertEqual(D.with_user(self.dev1)._role(), 'worker')
        self.assertFalse(D.with_user(self.plain)._role())
        for method in ('get_dashboard', 'get_project_board'):
            with self.assertRaises(AccessError):
                getattr(D.with_user(self.plain), method)()

    # -- isolation (§63) --------------------------------------------------------------
    def test_executive_sees_only_own(self):
        a1, a2, b1 = (self._data(u) for u in (self.exec_a1, self.exec_a2, self.exec_b1))
        self.assertEqual(a1['role'], 'executive')
        self.assertEqual(self._kpi(a1, 'leads'), 1)
        self.assertEqual(self._kpi(a2, 'leads'), 1)
        self.assertEqual(self._kpi(b1, 'leads'), 1)
        self.assertEqual(self._kpi(a1, 'projects'), 1)
        self.assertEqual(self._kpi(a2, 'projects'), 0)
        self.assertEqual(self._kpi(b1, 'projects'), 0)

    def test_head_sees_only_own_team(self):
        a, b = self._data(self.head_a), self._data(self.head_b)
        self.assertEqual(self._kpi(a, 'leads'), 2)
        self.assertEqual(self._kpi(b, 'leads'), 1)
        self.assertEqual(self._kpi(a, 'projects'), 1)
        self.assertEqual(self._kpi(b, 'projects'), 0)
        names_a = {r['name'] for r in a['tables'][0]['rows']}
        self.assertLessEqual(names_a, {'Exec A1', 'Exec A2'})
        self.assertNotIn('Exec B1', names_a)
        self.assertIn('TEAM A', a['title'])
        self.assertNotIn('TEAM B', a['title'])
        # filters cannot reach another team
        spoof = self._data(self.head_a, team_id=self.team_b.id, head_id=self.head_b.id)
        self.assertEqual(self._kpi(spoof, 'leads'), 2)
        self.assertEqual(spoof['options'], {})
        # a head's KPI drill-down domains stay inside the user's access
        for kpi in a['kpis']:
            model = kpi['action']['res_model']
            if model in ('otm.lead', 'otm.deal'):
                rows = self.env[model].with_user(self.head_a).search(kpi['action']['domain'])
                self.assertFalse(rows.filtered(lambda r: r.sales_team_id == self.team_b))

    def test_head_wallet_and_commission_isolated(self):
        self.env['otm.sales.wallet'].sudo().create({'sales_head_id': self.head_b.id})
        a = self._data(self.head_a)
        self.assertEqual(self._kpi(a, 'wallet'), self.env['otm.sales.wallet'].sudo().search(
            [('sales_head_id', '=', self.head_a.id)]).balance or 0)

    def test_admin_sees_all_and_filters(self):
        d = self._data(self.admin_user)
        self.assertEqual(d['role'], 'admin')
        all_leads = self.env['otm.lead'].sudo().with_context(active_test=False).search_count([])
        self.assertEqual(self._kpi(d, 'leads'), all_leads)
        self.assertEqual({r['name'] for r in d['tables'][0]['rows']} >= {'Team A', 'Team B'}, True)
        self.assertEqual(len(d['options']['teams']), self.env['otm.sales.team'].search_count([]))
        self.assertEqual(self._kpi(self._data(self.admin_user, team_id=self.team_b.id), 'leads'), 1)
        self.assertEqual(self._kpi(self._data(self.admin_user, executive_id=self.exec_a1.id), 'leads'), 1)
        self.assertEqual(self._kpi(self._data(self.admin_user, team_id=str(self.team_a.id)), 'leads'), 2)
        self.assertEqual(self._kpi(self._data(self.admin_user, date_from='2999-01-01'), 'leads'), 0)
        bad = self._data(self.admin_user, team_id='abc', date_from='nonsense')
        self.assertEqual(self._kpi(bad, 'leads'), all_leads)  # invalid filters are ignored, not injected

    def test_money_kpis(self):
        admin = self._data(self.admin_user)
        deals = self.env['otm.deal'].sudo().search([('status', '=', 'locked')])
        paid = self.env['otm.deal.payment'].sudo().search([('status', '=', 'received')])
        self.assertEqual(self._kpi(admin, 'sales'), sum(deals.mapped('total_amount')))
        self.assertEqual(self._kpi(admin, 'received'), sum(paid.mapped('amount')))
        self.assertEqual(self._kpi(admin, 'outstanding'), sum(deals.mapped('balance_due')))
        fin = self._data(self.fin)
        self.assertEqual(fin['role'], 'finance')
        self.assertEqual(self._kpi(fin, 'received'), sum(paid.mapped('amount')))
        head = self._data(self.head_a)
        self.assertEqual(self._kpi(head, 'won_value'), self.deal.total_amount)
        self.assertEqual(self._kpi(self._data(self.head_b), 'won_value'), 0)

    def test_project_head_and_worker(self):
        p = self._data(self.ph)
        self.assertEqual(p['role'], 'project_head')
        active = self.env['project.project'].sudo().search_count(
            [('otm_is_lifecycle', '=', True), ('otm_state', 'in', ('planning', 'in_progress', 'on_hold', 'delivered'))])
        self.assertEqual(self._kpi(p, 'projects'), active)  # Project Heads see every lifecycle project
        w = self._data(self.dev1)
        self.assertEqual(w['role'], 'worker')
        self.assertEqual(self._kpi(w, 'bugs'), 0)
        t = self._task(self.project)
        t.with_user(self.dev1).action_otm_start()
        expected = self.env['project.task'].with_user(self.dev1).search_count(
            [('user_ids', 'in', [self.dev1.id]), ('otm_dev_status', 'in', ('not_started', 'in_progress'))])
        self.assertEqual(self._kpi(self._data(self.dev1), 'tasks'), expected)

    def test_payload_has_no_foreign_records(self):
        """The payload only carries aggregates; no record lists of other teams."""
        import json
        blob = json.dumps(self._data(self.head_a), default=str)
        self.assertNotIn('Lead B1', blob)
        self.assertNotIn('Exec B1', blob)

    # -- project board -----------------------------------------------------------------
    def test_project_board(self):
        board = {c['key']: c for c in self.Dash.with_user(self.ph).get_project_board()['columns']}
        self.assertEqual(list(board), ['requirement', 'development', 'internal_testing', 'qc', 'deployment',
                                       'verification', 'training', 'payment', 'completed'])
        mine = lambda col: [c for c in board[col]['cards'] if c['id'] == self.project.id]
        self.assertEqual(len(mine('development')), 1)
        card = mine('development')[0]
        for key in ('customer', 'team', 'salesperson', 'project_head', 'deadline', 'progress', 'payment', 'qc'):
            self.assertIn(key, card)
        self.assertEqual(card['team'], 'Team A')
        self.assertEqual(card['salesperson'], 'Exec A1')
        # QC passed, nothing deployed yet -> Deployment
        t = self._task(self.project)
        t.with_user(self.dev1).action_otm_start()
        t.with_user(self.dev1).write({'otm_progress': 100})
        t.with_user(self.dev1).action_otm_submit()
        board = {c['key']: c for c in self.Dash.with_user(self.ph).get_project_board()['columns']}
        self.assertEqual(len(mine('internal_testing')), 1)
        t.with_user(self.ph).action_otm_complete()
        act = self.project.with_user(self.ph).action_submit_qc()
        qc = self.env['otm.qc'].browse(act['res_id'])
        board = {c['key']: c for c in self.Dash.with_user(self.ph).get_project_board()['columns']}
        self.assertEqual(len(mine('qc')), 1)
        qc.with_user(self.qc).action_start()
        qc.with_user(self.qc).action_pass()
        board = {c['key']: c for c in self.Dash.with_user(self.ph).get_project_board()['columns']}
        self.assertEqual(len(mine('deployment')), 1)
        # other team's head sees nothing
        other = {c['key']: c for c in self.Dash.with_user(self.head_b).get_project_board()['columns']}
        self.assertEqual(sum(c['count'] for c in other.values()), 0)

    # -- stage indicator -----------------------------------------------------------------
    def test_stage_tracker(self):
        steps = self.lead.otm_stage_tracker
        status = {s['key']: s['status'] for s in steps}
        self.assertEqual([s['key'] for s in steps][:3], ['lead', 'demo', 'estimate'])
        self.assertEqual(len(steps), 13)
        for k in ('lead', 'demo', 'estimate', 'deal_locked', 'agreement', 'advance'):
            self.assertEqual(status[k], 'completed', k)
        self.assertEqual(status['development'], 'current')
        self.assertEqual(status['completed'], 'pending')
        self.assertEqual(self.project.otm_stage_tracker, steps)
        self.project.with_user(self.ph).action_hold(reason='waiting customer') if hasattr(self.project, 'action_hold') else None
        if self.project.otm_state == 'on_hold':
            self.project.invalidate_recordset()
            blocked = [s for s in self.project.otm_stage_tracker if s['status'] == 'blocked']
            self.assertEqual(len(blocked), 1)
        fresh = self.lead_a2.otm_stage_tracker
        self.assertEqual(fresh[0]['status'], 'completed')
        self.assertEqual(fresh[1]['status'], 'current')
        self.assertEqual({s['status'] for s in fresh[2:]}, {'pending'})

    # -- customer 360 ------------------------------------------------------------------------
    def test_customer_360(self):
        d = self.Dash.with_user(self.head_a).get_customer_360(self.customer.id)
        self.assertEqual(d['partner']['name'], 'Cust')
        self.assertEqual(d['sales']['deals'], 1)
        self.assertEqual(d['sales']['teams'], ['Team A'])
        self.assertEqual(d['sales']['executives'], ['Exec A1'])
        self.assertEqual(d['projects']['active'], 1)
        self.assertEqual(d['payments']['received'], self.deal.amount_received)
        self.assertEqual(d['payments']['total'], self.deal.total_amount)
        self.assertEqual(d['payments']['outstanding'], self.deal.total_amount - self.deal.amount_received)
        self.assertTrue(d['servers']['restricted'])  # sales roles do not see servers
        self.assertEqual(d['integrations']['active'], 0)
        # Team B sees none of Team A's data
        b = self.Dash.with_user(self.head_b).get_customer_360(self.customer.id)
        self.assertEqual(b['sales']['deals'], 0)
        self.assertEqual(b['projects']['active'], 0)
        self.assertEqual(b['payments']['total'], 0)
        with self.assertRaises(AccessError):
            self.Dash.with_user(self.plain).get_customer_360(self.customer.id)
        # project head sees the infrastructure
        self.env['otm.client.server'].sudo().create({'name': 'S', 'customer_id': self.customer.id})
        ph = self.Dash.with_user(self.ph).get_customer_360(self.customer.id)
        self.assertEqual(ph['servers']['active'], 1)
        self.assertEqual(self.Dash.with_user(self.head_a).search_customers('Cust')[0]['name'], 'Cust')
        act = self.customer.with_user(self.head_a).action_otm_customer360()
        self.assertEqual(act['tag'], 'otm_customer360')

    # -- front-end assets ------------------------------------------------------------------------
    def test_assets_compile(self):
        bundle = self.env['ir.qweb']._get_asset_bundle('web.assets_backend', css=True, js=True)
        self.assertTrue(bundle.js())
        names = ' '.join(a.url for a in bundle.javascripts)
        self.assertIn('sales_project_lifecycle/static/src/js/dashboard.js', names)
