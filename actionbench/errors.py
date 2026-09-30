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


class ProviderOutputError(ActionBenchError):
    """Known, billed provider response that cannot be consumed as output."""


class AgentProtocolError(ActionBenchError):
    """The model's coordinator decision violates the frozen decision contract."""


class GeneratedProgramError(ActionBenchError):
    """A model-written program failed under a functioning execution harness."""
