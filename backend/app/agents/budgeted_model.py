from openai import APITimeoutError
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.exceptions import ModelHTTPError
from temporalio.exceptions import ApplicationError


class BudgetedModel(WrapperModel):
    """Every actual provider attempt reserves budget inside its Temporal activity."""

    async def request(self, messages, model_settings, model_request_parameters):
        from app.domain.common import DomainError
        from app.domain.execution import record_model_tokens, reserve_model_request

        settings = dict(model_settings or {})
        operation_id = settings.pop("learning_operation_id", None)
        if not operation_id:
            raise ApplicationError("Missing operation budget", type="ConfigurationError", non_retryable=True)
        try:
            reservation = await reserve_model_request(operation_id, limit=6)
        except DomainError:
            raise ApplicationError("Operation cannot issue another request", type="OperationBudgetClosed",
                                   non_retryable=True) from None
        failure = None
        try:
            model = self.wrapped
            if reservation["model_alias"] != model.model_name:
                model = OpenAIChatModel(reservation["model_alias"], provider=model.provider)
            response = await model.request(messages, settings, model_request_parameters)
        except (APITimeoutError, TimeoutError):
            failure = ApplicationError("Provider response timed out", type="ProviderTimeout")
        except ModelHTTPError as error:
            failure = ApplicationError(f"Provider request failed (HTTP {error.status_code})",
                type="ProviderTimeout" if error.status_code in {408, 504} else "ProviderRequestFailed",
                non_retryable=error.status_code in {400, 401, 403, 404, 422})
        except Exception:  # noqa: BLE001 -- provider exceptions may contain sensitive response payloads
            failure = ApplicationError("Provider request unavailable", type="ProviderRequestFailed")
        # Raise outside the handler so Temporal cannot serialize the original provider exception chain.
        if failure is not None:
            raise failure
        await record_model_tokens(operation_id, response.usage.input_tokens, response.usage.output_tokens)
        return response
