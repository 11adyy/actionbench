class ActionBenchError(Exception):
    """A user-facing benchmark failure."""


class ConfigurationError(ActionBenchError):
    pass


class BudgetExceeded(ActionBenchError):
    pass


class ResumeConflict(ActionBenchError):
    pass


class UnknownProviderOutcome(ActionBenchError):
    pass
