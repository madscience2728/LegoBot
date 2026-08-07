from dataclasses import dataclass

@dataclass
class NodeResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    parent_node_name: str | None = None
    # Set to "still_stuck_after_max_attempts" if refractory-recovery ran out
    # of attempts and emitted anyway rather than blocking the tick. None in
    # all other cases (including normal, non-refractory output).
    rumination_flag: str | None = None

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens