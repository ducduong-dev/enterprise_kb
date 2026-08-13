"""What differs between the two chat surfaces, in one place.

The internal bot answers a bank employee, on that employee's behalf, from whatever they are
entitled to see. The external bot answers a member of the public from published, externally
visible material and nothing else. Those are different products, and the temptation is to
build one with a flag.

This module is that flag, made explicit and total: every difference between the surfaces is a
field here, and both surfaces run the *same* pipeline. Nothing downstream branches on which
surface it is serving — it reads the policy. A difference that is not in this dataclass does
not exist, which is what makes "did we harden the external surface?" a question with an answer.

The access decisions themselves are **not** here. Who may see what is decided by
`kb_authz.FilterBuilder` from the verified principal (INV-2/3/4); a policy field that widened
retrieval would be a hole in the funnel. What lives here is conduct: how many passages to
gather, which prompt, how to refuse, what a caller may ask for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from kb_common.errors import ConfigError, PolicyViolation
from kb_schemas.enums import PrincipalKind

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


class Surface(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class SurfacePolicy:
    surface: Surface
    #: Principal kinds allowed to use this surface at all. An internal bot on the external
    #: surface would answer a member of the public with an employee's visibility.
    allowed_kinds: frozenset[PrincipalKind]
    prompt_file: str
    #: Passages gathered per question. The external surface takes fewer: its corpus is small,
    #: and a wide net there mostly adds material for an injection attempt to work with.
    top_k: int
    #: Answer length. External answers are deliberately short — a public answer that runs long
    #: starts explaining, and explaining is where an unapproved commitment gets made.
    max_answer_tokens: int
    #: Turns of history kept for condensation. The rest is dropped, not summarized: a summary
    #: of an attacker's earlier turns is an injection payload with a shorter path.
    history_turns: int
    #: Requests per principal per minute. The external surface is a public endpoint.
    rate_limit_per_minute: int
    #: What the bot says when the context does not answer the question. Fixed text, not
    #: generated: a model asked to explain why it cannot answer will speculate about the
    #: answer, which is the failure the refusal exists to prevent.
    refusal: str
    #: Whether graph expansion is requested. Internal reviewers benefit from "see also"; an
    #: external answer citing a document nobody asked about is scope creep.
    expand_graph: bool
    #: Whether an answer may leave without passing the PII filter. Never true for the public
    #: surface: an answer to a member of the public is the one place where a leaked identifier
    #: cannot be recalled, apologised for, or contained by an ACL (INV-7).
    output_filter_required: bool = True

    def prompt(self) -> str:
        return (PROMPT_DIR / self.prompt_file).read_text(encoding="utf-8")

    def check_output_filter(self, detector: object) -> None:
        """Refuse to serve without a working filter.

        Not a configuration check — a refusal to start answering. A detector that reports
        itself unhealthy, or a stand-in that declares `real_rules: False`, would let the public
        surface answer with the filter *present and doing nothing*, which looks identical in
        every log and dashboard we have.
        """
        info = getattr(detector, "info", None)
        healthy = bool(getattr(detector, "health", lambda: False)())
        real = bool(getattr(info, "extra", {}).get("real_rules", True)) if info else False
        if not self.output_filter_required:
            return
        if info is None or not healthy or not real:
            raise ConfigError(
                "the external surface requires a working PII output filter",
                surface=self.surface.value,
                detector=getattr(info, "name", "none"),
                invariant="INV-7",
            )

    def check_principal(self, kind: PrincipalKind) -> None:
        if kind not in self.allowed_kinds:
            raise PolicyViolation(
                f"{kind.value} may not use the {self.surface.value} chat surface",
                surface=self.surface.value,
                principal_kind=kind.value,
                invariant="INV-3" if self.surface is Surface.INTERNAL else "INV-4",
            )


INTERNAL = SurfacePolicy(
    surface=Surface.INTERNAL,
    # A user signed into the portal, or the bot carrying that user's exchanged token. The bot's
    # own service account is deliberately absent: it has zero document visibility (INV-3), so
    # letting it in would produce a confident answer built from an empty context.
    allowed_kinds=frozenset({PrincipalKind.USER, PrincipalKind.INTERNAL_BOT}),
    prompt_file="grounded_answer_internal.md",
    top_k=8,
    max_answer_tokens=800,
    history_turns=6,
    rate_limit_per_minute=30,
    # The internal surface filters too (INV-7); it is not *structurally* refused without a
    # detector, because an employee reading their own department's document is a different
    # exposure from a stranger reading it on the internet.
    output_filter_required=False,
    refusal=(
        "Tôi không tìm thấy căn cứ trong kho tài liệu bạn được phép truy cập để trả lời câu "
        "hỏi này. Bạn có thể hỏi lại bằng từ ngữ khác, hoặc liên hệ đơn vị chủ quản của văn "
        "bản liên quan."
    ),
    expand_graph=True,
)

EXTERNAL = SurfacePolicy(
    surface=Surface.EXTERNAL,
    allowed_kinds=frozenset({PrincipalKind.EXTERNAL_BOT}),
    prompt_file="grounded_answer_external.md",
    top_k=5,
    max_answer_tokens=400,
    history_turns=4,
    rate_limit_per_minute=10,
    refusal=(
        "Xin lỗi, tôi chưa có thông tin công bố để trả lời câu hỏi này. Quý khách vui lòng "
        "liên hệ hotline hoặc chi nhánh gần nhất để được hỗ trợ."
    ),
    expand_graph=False,
)

POLICIES: dict[Surface, SurfacePolicy] = {
    Surface.INTERNAL: INTERNAL,
    Surface.EXTERNAL: EXTERNAL,
}


def policy_for(surface: str) -> SurfacePolicy:
    try:
        return POLICIES[Surface(surface)]
    except ValueError as exc:
        raise PolicyViolation("unknown chat surface", surface=surface) from exc
