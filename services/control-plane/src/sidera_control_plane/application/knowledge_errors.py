class KnowledgeAdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        temporary: bool,
        status_code: int | None = None,
    ) -> None:
        self.code = code
        self.temporary = temporary
        self.status_code = status_code
        super().__init__(message)
