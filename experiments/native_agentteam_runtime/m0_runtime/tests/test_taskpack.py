try:
    from .taskpack_test_support import *
    from .taskpack_test_cases_authoring import AuthoringMixin
    from .taskpack_test_cases_blueprints import BlueprintsMixin
    from .taskpack_test_cases_cli import CliMixin
    from .taskpack_test_cases_contracts import ContractsMixin
    from .taskpack_test_cases_controllers import ControllersMixin
    from .taskpack_test_cases_core import CoreMixin
    from .taskpack_test_cases_governance import GovernanceMixin
    from .taskpack_test_cases_notifications import NotificationsMixin
    from .taskpack_test_cases_projections import ProjectionsMixin
    from .taskpack_test_cases_pursue import PursueMixin
    from .taskpack_test_cases_readiness import ReadinessMixin
    from .taskpack_test_cases_release import ReleaseMixin
    from .taskpack_test_cases_reporting import ReportingMixin
    from .taskpack_test_cases_validation import ValidationMixin
except ImportError:
    from taskpack_test_support import *
    from taskpack_test_cases_authoring import AuthoringMixin
    from taskpack_test_cases_blueprints import BlueprintsMixin
    from taskpack_test_cases_cli import CliMixin
    from taskpack_test_cases_contracts import ContractsMixin
    from taskpack_test_cases_controllers import ControllersMixin
    from taskpack_test_cases_core import CoreMixin
    from taskpack_test_cases_governance import GovernanceMixin
    from taskpack_test_cases_notifications import NotificationsMixin
    from taskpack_test_cases_projections import ProjectionsMixin
    from taskpack_test_cases_pursue import PursueMixin
    from taskpack_test_cases_readiness import ReadinessMixin
    from taskpack_test_cases_release import ReleaseMixin
    from taskpack_test_cases_reporting import ReportingMixin
    from taskpack_test_cases_validation import ValidationMixin


class TaskpackTests(
    AuthoringMixin,
    BlueprintsMixin,
    CliMixin,
    ContractsMixin,
    ControllersMixin,
    CoreMixin,
    GovernanceMixin,
    NotificationsMixin,
    ProjectionsMixin,
    PursueMixin,
    ReadinessMixin,
    ReleaseMixin,
    ReportingMixin,
    ValidationMixin,
    unittest.TestCase,
):
    pass


if __name__ == "__main__":
    unittest.main()
