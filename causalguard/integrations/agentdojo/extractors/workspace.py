"""Runtime provenance for the first AgentDojo workspace read/send path."""

from __future__ import annotations

from collections.abc import Sequence

from agentdojo.default_suites.v1.tools.types import CloudDriveFile, Email

from causalguard.integrations.agentdojo.common import stable_hash
from causalguard.integrations.agentdojo.extractors.base import (
    DomainObjectEvidence,
    DomainOperationEvidence,
    ToolExecutionContext,
)
from causalguard.schema.events import EventType


class WorkspaceReadSendExtractor:
    """Extract drive reads and email sends used by workspace/user_task_33."""

    _SUPPORTED = frozenset({"search_files_by_filename", "send_email"})

    def supports(self, function_name: str) -> bool:
        return function_name in self._SUPPORTED

    def extract(
        self,
        context: ToolExecutionContext,
    ) -> Sequence[DomainOperationEvidence]:
        if context.error is not None:
            return ()
        if context.function_name == "search_files_by_filename":
            return self._extract_file_search(context)
        if context.function_name == "send_email":
            return self._extract_email_send(context)
        return ()

    def _extract_file_search(
        self,
        context: ToolExecutionContext,
    ) -> Sequence[DomainOperationEvidence]:
        if not isinstance(context.result, Sequence) or isinstance(
            context.result,
            (str, bytes, bytearray),
        ):
            return ()
        files = tuple(
            _file_evidence(item)
            for item in context.result
            if isinstance(item, CloudDriveFile)
        )
        if not files:
            return ()
        return (
            DomainOperationEvidence(
                event_type=EventType.DATA_ACCESS,
                operation_type="file_read",
                action_class=context.function_name,
                read_objects=files,
                target_resource=files[0].reference if len(files) == 1 else None,
            ),
        )

    def _extract_email_send(
        self,
        context: ToolExecutionContext,
    ) -> Sequence[DomainOperationEvidence]:
        if not isinstance(context.result, Email):
            return ()

        email = _email_evidence(context.result)
        content = _email_content_evidence(context.result)
        attachments = self._attachment_evidence(context)
        payload_objects = (content, *attachments)
        destination = _email_destination(context.result)
        byte_count = len(context.result.body.encode("utf-8")) + sum(
            item.size for item in self._attachment_files(context)
        )
        return (
            DomainOperationEvidence(
                event_type=EventType.NETWORK_SEND,
                operation_type="email_send",
                action_class=context.function_name,
                write_objects=(email,),
                payload_objects=payload_objects,
                tool_input_objects=payload_objects,
                llm_generated_objects=(content,),
                destination=destination,
                target_resource=email.reference,
                byte_count=byte_count,
            ),
        )

    def _attachment_files(
        self,
        context: ToolExecutionContext,
    ) -> tuple[CloudDriveFile, ...]:
        environment = context.environment_before
        if environment is None or not hasattr(environment, "cloud_drive"):
            return ()
        cloud_drive = environment.cloud_drive
        files = []
        for attachment_id in context.result.attachments:
            if not isinstance(attachment_id, str):
                continue
            file = cloud_drive.files.get(attachment_id)
            if file is not None:
                files.append(file)
        return tuple(files)

    def _attachment_evidence(
        self,
        context: ToolExecutionContext,
    ) -> tuple[DomainObjectEvidence, ...]:
        return tuple(_file_evidence(item) for item in self._attachment_files(context))


def _file_evidence(file: CloudDriveFile) -> DomainObjectEvidence:
    reference = f"agentdojo:workspace:file:{file.id_}"
    version = stable_hash(
        {
            "id": file.id_,
            "filename_hash": stable_hash(file.filename),
            "content_hash": stable_hash(file.content),
            "owner": str(file.owner),
            "last_modified": file.last_modified,
            "shared_with": file.shared_with,
            "size": file.size,
        }
    )
    return DomainObjectEvidence(
        reference=reference,
        node_id=_versioned_node_id(reference, version),
        object_kind="cloud_drive_file",
        content_hash=stable_hash(file.content),
        version=version,
        owner=str(file.owner),
    )


def _email_evidence(email: Email) -> DomainObjectEvidence:
    reference = f"agentdojo:workspace:email:{email.id_}"
    version = stable_hash(email)
    return DomainObjectEvidence(
        reference=reference,
        node_id=_versioned_node_id(reference, version),
        object_kind="email",
        content_hash=stable_hash(email),
        version=version,
        owner=str(email.sender),
    )


def _email_content_evidence(email: Email) -> DomainObjectEvidence:
    reference = f"agentdojo:workspace:email_content:{email.id_}"
    version = stable_hash({"subject": email.subject, "body": email.body})
    return DomainObjectEvidence(
        reference=reference,
        node_id=_versioned_node_id(reference, version),
        object_kind="email_content",
        content_hash=version,
        version=version,
        owner=str(email.sender),
    )


def _email_destination(email: Email) -> str:
    recipients = [*email.recipients, *email.cc, *email.bcc]
    return ",".join(f"mailto:{str(item).lower()}" for item in recipients)


def _versioned_node_id(reference: str, version: str) -> str:
    digest = version.removeprefix("sha256:")
    return f"data:{reference}:version:{digest}"
