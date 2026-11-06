from enum import Enum


class ProjectStatus(str, Enum):
    PENDING = "pending"  # 待整理
    READY = "ready"  # 待分析
    ANALYZING = "analyzing"  # 分析中
    ANALYZED = "analyzed"  # 分析完成
    ARCHIVED = "archived"  # 已归档


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
