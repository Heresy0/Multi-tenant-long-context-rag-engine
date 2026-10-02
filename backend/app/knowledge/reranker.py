from dataclasses import dataclass

import requests
from pydantic import BaseModel, SecretStr


class RerankError(RuntimeError):
    """重排序服务调用失败。"""


@dataclass(frozen=True)
class RerankResult:
    index: int
    relevance_score: float


class BaseReranker(BaseModel):
    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[RerankResult]:
        raise NotImplementedError


class QwenReranker(BaseReranker):
    endpoint: str
    api_key: SecretStr
    model: str = "qwen3-rerank"
    timeout_seconds: float = 5.0

    def rerank(
            self,
            *,
            query: str,
            documents: list[str],
            top_n: int,
    ) -> list[RerankResult]:
        if not documents:
            return []

        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n,len(documents)),
            "instruct":(
                "Given a user question, retrieve passages "
                "that contain the evidence needed to answer it."
            ),
        }

        try:
             response = requests.post(
                 self.endpoint,

                 headers={
                     "Authorization":(
                         "Bearer "
                         f"{self.api_key.get_secret_value()}"
                     ),
                     "Content-Type":"application/json",
                 },
                 json=payload,
                 timeout=self.timeout_seconds,
             )
             response.raise_for_status()
             data = response.json()
        except requests.RequestException as exc:
            raise RerankError(
                f"重排序接口调用失败：{exc}"
            )from exc
        except ValueError as exc:
            raise RerankError(
                "重排序接口返回了无效 JSON"
            ) from exc

        raw_results = data.get("results")

        if not isinstance(raw_results,list):
            raise RerankError(
                "重排序结果缺少results"
            )

        results: list[RerankResult] = []
        seen_indices: set[int] = set()

        for item in raw_results:
            if not isinstance(item, dict):
                raise RerankError(
                    "重排序结果格式错误"
                )

            index = item.get("index")
            score = item.get("relevance_score")

            if(
                type(index) is not int
                or index < 0
                or index >= len(documents)
            ):
                raise RerankError(
                    f"重排序结果下标无效：{index}"
                )

            if index in seen_indices:
                raise RerankError(
                    f"重排序结果下标重复：{index}"
                )

            if not isinstance(score,(int, float)):
                raise RerankError(
                    "重排序结果缺少相关性分数"
                )

            seen_indices.add(index)

            results.append(
                RerankResult(
                    index=index,
                    relevance_score=float(score),
                )
            )

        if not results:
            raise RerankError(
                "重排序接口没有返回任何结果"
            )

        return results
