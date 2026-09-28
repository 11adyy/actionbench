class ActionBenchError(Exception):
    """A user-facing benchmark failure."""


class ConfigurationError(ActionBenchError):
    pass


class BudgetExceeded(ActionBenchError):
    pass


class CampaignBudgetExceeded(BudgetExceeded):
    """The study is out of money; remaining work is pending, not failed."""


class InfrastructureError(ActionBenchError):
    """An external service or evaluator failed independently of the agent."""


class ResumeConflict(ActionBenchError):
    pass


class UnknownProviderOutcome(ActionBenchError):
    pass
