from typing import Optional, List, Literal
from pydantic import BaseModel, HttpUrl, Field


class CandidateScore(BaseModel):
    method: str
    score: float
    len: int

class PageResult(BaseModel):
    engine: Optional[str] = None
    title: Optional[str] = None
    url: Optional[HttpUrl] = None
    final_url: Optional[HttpUrl] = None

    page_title: Optional[str] = None

    method: Optional[Literal[
        "github_issue", 
        "selectors",
        "main_block",
        "readability",
        "body",
    ]] = None

    score: Optional[float] = None
    candidates: Optional[List[CandidateScore]] = None

    text: str = ""
    error: Optional[str] = None

    @property
    def is_good(self) -> bool:
        # 建议稍微放宽一点，对 GitHub issue 很重要
        if self.error is not None:
            return False
        if self.method == "github_issue":
            return len(self.text) > 160
        return len(self.text) > 256

