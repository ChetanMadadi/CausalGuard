"""Opt-in controlled test extension; NOT a stock AgentDojo benchmark tool."""
from __future__ import annotations

from dataclasses import replace
from typing import Annotated

from agentdojo.default_suites.v1.tools.cloud_drive_client import CloudDrive
from agentdojo.functions_runtime import Depends

from causalguard.integrations.agentdojo.extractors.base import DomainOperationEvidence
from causalguard.integrations.agentdojo.extractors.workspace import (
    WorkspaceReadSendExtractor, _file_evidence,
)
from causalguard.schema.events import EventType


def controlled_copy_file(
    cloud_drive: Annotated[CloudDrive, Depends("cloud_drive")],
    source_id: str,
) -> str:
    """Copy a file's complete contents to a fresh drive resource.

    :param source_id: Existing source file ID.
    """
    source = cloud_drive.get_file_by_id(source_id)
    output = cloud_drive.create_file("controlled-copy.txt", source.content)
    return str(output.id_)


class ControlledCopyExtractor(WorkspaceReadSendExtractor):
    """Attest only this implemented identity-copy mechanism and snapshot roots.

    Version-specific root identities are captured from the initial environment.
    Unknown/new versions are never silently treated as roots. No labels propagate.
    """

    def __init__(self, environment):
        self.root_nodes = frozenset(
            _file_evidence(file).node_id for file in environment.cloud_drive.files.values()
        )
        self.copy_nodes = set()

    def supports(self, function_name):
        return function_name == "controlled_copy_file" or super().supports(function_name)

    def _lineage(self, item):
        status = ("copy_output" if item.node_id in self.copy_nodes
                  else "root" if item.node_id in self.root_nodes else "unrecorded")
        return replace(item, lineage_status=status)

    def propose(self, context):
        proposed = super().propose(context)
        if proposed is None:
            return None
        return replace(proposed, tool_input_objects=tuple(
            self._lineage(item) for item in proposed.tool_input_objects
        ))

    def extract(self, context):
        if context.function_name != "controlled_copy_file":
            return super().extract(context)
        if context.error is not None or context.environment_before is None or context.environment_after is None:
            return ()
        source = context.environment_before.cloud_drive.files[context.arguments["source_id"]]
        output = context.environment_after.cloud_drive.files[str(context.result)]
        # Verify runtime effects, not a tool's claim about its transformation.
        if (source.content != output.content or source.id_ == output.id_
                or output.id_ in context.environment_before.cloud_drive.files):
            return ()
        output_evidence = _file_evidence(output)
        self.copy_nodes.add(output_evidence.node_id)
        return (DomainOperationEvidence(
            event_type=EventType.SYSTEM_OPERATION,
            operation_type="controlled_file_copy", action_class="controlled_copy_file",
            data_flow_semantics="identity_copy_v1",
            read_objects=(self._lineage(_file_evidence(source)),),
            write_objects=(self._lineage(output_evidence),),
        ),)
