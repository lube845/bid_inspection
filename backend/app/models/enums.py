from enum import Enum


class ProjectStatus(str, Enum):
    PENDING = "pending"  # 待整理
    READY = "ready"  # 待分析
    ANALYZING = "analyzing"  # 分析中
    ANALYZED = "analyzed"  # 分析完成
    # 分析任务已完成，但某个维度没有完整执行（例如价格维度未识别到报价）。
    # 单独成状态而不是沿用「分析完成」，避免用户误以为三个维度都有结论。
    ANALYZED_PARTIAL = "analyzed_partial"  # 分析完成（部分维度未覆盖）
    ARCHIVED = "archived"  # 已归档


# 项目状态文案的唯一来源：服务端下发，前端不再维护第二套字典
PROJECT_STATUS_LABELS = {
    ProjectStatus.PENDING.value: "准备中",
    ProjectStatus.READY.value: "待分析",
    ProjectStatus.ANALYZING.value: "分析中",
    ProjectStatus.ANALYZED.value: "分析完成",
    ProjectStatus.ANALYZED_PARTIAL.value: "分析完成（部分维度未覆盖）",
    ProjectStatus.ARCHIVED.value: "已归档",
}


class FileKind(str, Enum):
    TENDER = "tender"  # 招标文件
    BID = "bid"  # 投标企业文件


class FileParseStatus(str, Enum):
    UPLOADING = "uploading"  # 上传中
    PENDING = "pending"  # 待解析
    PARSING = "parsing"  # 解析中
    SUCCESS = "success"  # 解析成功
    PARTIAL = "partial"  # 部分成功
    FAILED = "failed"  # 解析失败
    SUPERSEDED = "superseded"  # 已失效（被替换）


class PageStatus(str, Enum):
    SUCCESS = "success"
    EMPTY = "empty"
    FAILED = "failed"


class ExtractionMethod(str, Enum):
    NATIVE = "native"
    OCR = "ocr"
    NONE = "none"


class CompanyStatus(str, Enum):
    INCOMPLETE = "incomplete"  # 待完善
    PENDING_PARSE = "pending_parse"  # 待解析
    PARSING = "parsing"  # 解析中
    PENDING_CONFIRM = "pending_confirm"  # 待确认
    ANALYZABLE = "analyzable"  # 可分析
    ABNORMAL = "abnormal"  # 资料异常


class LogicalDocStatus(str, Enum):
    NOT_GENERATED = "not_generated"
    GENERATED = "generated"
    OUTDATED = "outdated"


class TaskStatus(str, Enum):
    QUEUED = "queued"  # 排队中
    RUNNING = "running"  # 执行中
    PARTIAL = "partial"  # 部分完成
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 执行失败
    CANCELLED = "cancelled"  # 已取消


class PairStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Dimension(str, Enum):
    TEXT = "text"
    PERSONNEL = "personnel"
    PRICE = "price"


class EvidenceType(str, Enum):
    TEXT = "text"
    PERSONNEL = "personnel"
    PRICE = "price"
    # 文件指纹：同一份原始文件被两家以上企业提交（铁证，不依赖合同内容）
    FILE = "file"
    # 文档元数据：PDF 生产者/作者、DOCX 作者与最后修改人、生成环境指纹
    METADATA = "metadata"

    @classmethod
    def analysis_types(cls) -> list[str]:
        """不进入「分析维度」菜单、由文件阶段独立产出的证据类型。"""
        return [cls.FILE.value, cls.METADATA.value]


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewStatus(str, Enum):
    PENDING = "pending"  # 待审核
    CONFIRMED = "confirmed"  # 确认异常线索
    EXCLUDED = "excluded"  # 正常 / 排除
    UNDECIDED = "undecided"  # 暂不判断


class UserRole(str, Enum):
    ADMIN = "admin"
    ANALYST = "analyst"
    REVIEWER = "reviewer"


class RuleStatus(str, Enum):
    DRAFT = "draft"  # 待生效
    ACTIVE = "active"  # 已生效
    ARCHIVED = "archived"  # 历史版本（已被新版本取代）
