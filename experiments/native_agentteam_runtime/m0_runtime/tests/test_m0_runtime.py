try:
    from .m0_runtime_test_support import *
    from .m0_runtime_test_cases_adapters import AdaptersMixin
    from .m0_runtime_test_cases_core import CoreMixin
    from .m0_runtime_test_cases_grounding import GroundingMixin
    from .m0_runtime_test_cases_integration import IntegrationMixin
    from .m0_runtime_test_cases_mailbox import MailboxMixin
    from .m0_runtime_test_cases_notifications import NotificationsMixin
    from .m0_runtime_test_cases_observability import ObservabilityMixin
    from .m0_runtime_test_cases_planning import PlanningMixin
    from .m0_runtime_test_cases_reports import ReportsMixin
    from .m0_runtime_test_cases_retry import RetryMixin
    from .m0_runtime_test_cases_scheduler import SchedulerMixin
    from .m0_runtime_test_cases_workers import WorkersMixin
except ImportError:
    from m0_runtime_test_support import *
    from m0_runtime_test_cases_adapters import AdaptersMixin
    from m0_runtime_test_cases_core import CoreMixin
    from m0_runtime_test_cases_grounding import GroundingMixin
    from m0_runtime_test_cases_integration import IntegrationMixin
    from m0_runtime_test_cases_mailbox import MailboxMixin
    from m0_runtime_test_cases_notifications import NotificationsMixin
    from m0_runtime_test_cases_observability import ObservabilityMixin
    from m0_runtime_test_cases_planning import PlanningMixin
    from m0_runtime_test_cases_reports import ReportsMixin
    from m0_runtime_test_cases_retry import RetryMixin
    from m0_runtime_test_cases_scheduler import SchedulerMixin
    from m0_runtime_test_cases_workers import WorkersMixin


class M0RuntimeTests(
    AdaptersMixin,
    CoreMixin,
    GroundingMixin,
    IntegrationMixin,
    MailboxMixin,
    NotificationsMixin,
    ObservabilityMixin,
    PlanningMixin,
    ReportsMixin,
    RetryMixin,
    SchedulerMixin,
    WorkersMixin,
    unittest.TestCase,
):
    pass


if __name__ == "__main__":
    unittest.main()
