"""Brain module (WS3): understand the user, pick at most one action, reply in their language.

Public API (import from the submodules; this package file stays import-free so that
``brain.memory`` can be used without loading the LLM client):

* :class:`brain.brain.SarvamBrain` - the :class:`core.interfaces.Brain` implementation.
* :class:`brain.llm_client.SarvamLLM` - Sarvam chat completions with a per-request timeout.
* :class:`brain.memory.Memory` - SQLite conversation memory.
* :class:`brain.fake.FakeLLM` / :class:`brain.fake.FakeBrain` - offline fakes.
"""
