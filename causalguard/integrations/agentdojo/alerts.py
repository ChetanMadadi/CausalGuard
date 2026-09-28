"""Post-tool-result observation; delegates every enforcement decision unchanged."""
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.tool_execution import ToolsExecutor


class PostCommitAlertExecutor(BasePipelineElement):
    """Replays committed tool-result prefixes after the original executor returns.

    A batched executor may already have executed the whole batch. Alerts are
    observational, never precommit guards or evidence of prevention.
    """
    name = "postcommit-path-alerts"

    def __init__(self, delegate, callback):
        self.delegate, self.callback = delegate, callback

    def query(self, query, runtime, env, messages=[], extra_args={}):
        result = self.delegate.query(query, runtime, env, messages, extra_args)
        final_query, _, _, final_messages, _ = result
        for index in range(len(messages), len(final_messages)):
            if final_messages[index]["role"] == "tool":
                self.callback(final_query, final_messages[:index + 1], f"tool-result:{index}")
        return result


def install_postcommit_alerts(pipeline, callback):
    if not hasattr(pipeline, "elements"):
        raise ValueError("alert integration requires an inspectable AgentDojo tool pipeline")
    count = 0
    elements = []
    for element in pipeline.elements:
        if isinstance(element, PostCommitAlertExecutor):
            raise ValueError("pipeline already has a postcommit alert observer")
        if isinstance(element, ToolsExecutor):
            elements.append(PostCommitAlertExecutor(element, callback))
            count += 1
        else:
            if hasattr(element, "elements"):
                count += install_postcommit_alerts(element, callback)
            elements.append(element)
    pipeline.elements = elements
    return count
