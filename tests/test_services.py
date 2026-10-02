from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import LifecycleCommon


@tagged('post_install', '-at_install', 'otm_security')
class TestServices(LifecycleCommon):

    def test_line_defaults_and_subtotal(self):
        line = self.env['otm.lead.service.line'].with_user(self.exec_a1).create({
            'lead_id': self.lead_a1.id, 'service_id': self.service.id, 'quantity': 3})
        self.assertEqual(line.base_unit_price, 20000)
        self.assertEqual(line.base_subtotal, 60000)
        line.base_unit_price = 18000.5
        self.assertEqual(line.base_subtotal, 54001.5)

    def test_lead_base_total(self):
        Line = self.env['otm.lead.service.line']
        hrms = self.env['otm.service'].create({'name': 'HRMS', 'code': 'TST-HR', 'base_amount': 30000})
        web = self.env['otm.service'].create({'name': 'Web', 'code': 'TST-WEB', 'base_amount': 25000})
        for svc in (self.service, hrms, web):
            Line.with_user(self.exec_a1).create({'lead_id': self.lead_a1.id, 'service_id': svc.id})
        # the fixture already holds one TST-CRM line (20,000)
        self.assertEqual(self.lead_a1.base_total, 95000)
        self.assertEqual(self.lead_a1.service_count, 4)

    def test_quantity_must_be_positive(self):
        with self.assertRaises(ValidationError):
            self.env['otm.lead.service.line'].create({
                'lead_id': self.lead_a1.id, 'service_id': self.service.id, 'quantity': 0})

    def test_service_code_unique(self):
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), self.cr.savepoint():
            self.env['otm.service'].create({'name': 'Dup', 'code': 'TST-CRM'})

    def test_service_admin_only_writes(self):
        with self.assertRaises(AccessError):
            self.service.with_user(self.head_a).write({'base_amount': 1})
        self.service.with_user(self.admin_user).write({'base_amount': 21000})
        self.assertEqual(self.service.base_amount, 21000)
        # executives can read the master
        self.assertEqual(self.service.with_user(self.exec_a1).base_amount, 21000)

    def test_recurring_requires_period(self):
        with self.assertRaises(ValidationError):
            self.env['otm.service'].create({'name': 'R', 'code': 'TST-R', 'recurring': True})

    def test_negative_base_amount_rejected(self):
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), self.cr.savepoint():
            self.env['otm.service'].create({'name': 'N', 'code': 'TST-N', 'base_amount': -1})
