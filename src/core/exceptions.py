"""
RPA 自定义异常
"""

class RPAException(Exception):
    """RPA 基础异常"""
    pass


class WorkerException(RPAException):
    """Worker 层异常"""
    pass


class ElementNotFoundError(WorkerException):
    """UI 元素未找到"""
    pass


class OperationTimeoutError(WorkerException):
    """操作超时"""
    pass


class ApplicationNotReadyError(WorkerException):
    """应用程序未就绪"""
    pass


class FlowException(RPAException):
    """Flow 层异常"""
    pass


class FlowStepFailedError(FlowException):
    """流程步骤失败"""
    def __init__(self, step_name: str, reason: str):
        self.step_name = step_name
        self.reason = reason
        super().__init__(f"Step '{step_name}' failed: {reason}")


# ==================== Vision Exceptions ====================

class VisionException(RPAException):
    """Vision/AI feature base exception"""
    pass


class ScreenshotComparisonError(VisionException):
    """Screenshot comparison failed"""
    pass


class VLMError(VisionException):
    """Vision Language Model error"""
    pass


class VLMProviderNotFoundError(VLMError):
    """VLM provider not found or not configured"""
    pass


class VLMTimeoutError(VLMError):
    """VLM request timed out"""
    pass


class VLMResponseError(VLMError):
    """VLM returned invalid or unexpected response"""
    pass


class OCRError(VisionException):
    """OCR processing error"""
    pass


class OCRProviderNotFoundError(OCRError):
    """OCR provider not found or not configured"""
    pass


class JSONRepairError(RPAException):
    """JSON repair failed"""
    pass


class PageObjectError(RPAException):
    """Page object navigation/detection error"""
    pass


class PageNotFoundError(PageObjectError):
    """Expected page not found"""
    pass
