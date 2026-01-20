"""
GDS2 Report Parser - HTML 报告解析器

负责：
1. 查找最新的报告文件
2. 解析不同类型的报告（DTC、PID等）
3. 返回结构化数据

报告文件位置：C:/Users/<user>/AppData/Local/Temp/GDS 2/
"""

import os
import re
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class DTCInfo:
    """故障码信息"""
    code: str                    # 故障码，如 "P0300"
    description: str = ""        # 故障描述
    symptom_description: str = ""  # 症状描述
    status: str = ""             # 状态（当前/历史/待定）
    module: str = ""             # ECU 模块名称
    symptom_byte: str = ""       # 症状字节
    dtc_type: str = ""           # DTC 类型
    freeze_frame: Dict = None    # 冻结帧数据

    def to_dict(self) -> Dict:
        return {
            "code": self.code,
            "description": self.description,
            "symptom_description": self.symptom_description,
            "status": self.status,
            "module": self.module,
            "symptom_byte": self.symptom_byte,
            "dtc_type": self.dtc_type,
            "freeze_frame": self.freeze_frame,
        }


@dataclass
class ModuleStatus:
    """模块状态信息"""
    name: str = ""
    status: str = ""       # "OK", "DTCs Stored", "No Communication", "Lost Communication"
    dtc_count: int = 0
    dlc_pin: str = ""

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "status": self.status,
            "dtc_count": self.dtc_count,
            "dlc_pin": self.dlc_pin,
        }


@dataclass
class VehicleInfo:
    """车辆信息"""
    vin: str = ""
    make: str = ""
    model: str = ""
    year: str = ""
    transmission: str = ""
    engine: str = ""

    def to_dict(self) -> Dict:
        return {
            "vin": self.vin,
            "make": self.make,
            "model": self.model,
            "year": self.year,
            "transmission": self.transmission,
            "engine": self.engine,
        }


class DTCReportParser:
    """
    GDS2 HTML 报告解析器 (Alias for GDS2ReportParser)
    """
    # Alias - same as GDS2ReportParser
    pass


class GDS2ReportParser:
    """
    GDS2 HTML 报告解析器

    用于解析 GDS2 生成的 HTML 报告文件，提取结构化数据。

    使用示例:
        parser = GDS2ReportParser()

        # 查找最新报告
        report_path = parser.find_latest_report("DTC Display")

        # 解析 DTC 报告
        result = parser.parse_dtc_report(report_path)
        print(f"Found {len(result['dtc_list'])} DTCs")
    """

    # 默认报告目录
    DEFAULT_REPORT_DIR = Path(os.environ.get('LOCALAPPDATA', '')) / 'Temp' / 'GDS 2'

    def __init__(self, report_dir: Optional[str] = None):
        """
        初始化解析器

        Args:
            report_dir: 报告目录路径，默认为 GDS2 临时目录
        """
        self.report_dir = Path(report_dir) if report_dir else self.DEFAULT_REPORT_DIR
        self.logger = logging.getLogger(__name__)

    def find_latest_report(self, report_type: str = "DTC Display") -> Optional[str]:
        """
        查找最新的报告文件

        Args:
            report_type: 报告类型，如 "DTC Display", "Data Display" 等

        Returns:
            最新报告文件的路径，未找到返回 None
        """
        if not self.report_dir.exists():
            self.logger.warning(f"Report directory not found: {self.report_dir}")
            return None

        # 查找匹配的文件
        pattern = f"{report_type}_*.html"
        files = list(self.report_dir.glob(pattern))

        if not files:
            self.logger.warning(f"No reports found matching: {pattern}")
            return None

        # 返回最新的文件
        latest = max(files, key=lambda f: f.stat().st_mtime)
        self.logger.info(f"Found latest report: {latest}")
        return str(latest)

    def find_new_report(self, report_type: str, existing_files: set) -> Optional[str]:
        """
        查找新创建的报告文件（排除已存在的文件）

        Args:
            report_type: 报告类型
            existing_files: 已存在文件的集合

        Returns:
            新报告文件的路径
        """
        if not self.report_dir.exists():
            return None

        pattern = f"{report_type}_*.html"
        current_files = set(self.report_dir.glob(pattern))
        new_files = current_files - existing_files

        if new_files:
            return str(max(new_files, key=lambda f: f.stat().st_mtime))

        return None

    def get_existing_reports(self, report_type: str = "DTC Display") -> set:
        """
        获取现有的报告文件集合

        Args:
            report_type: 报告类型

        Returns:
            文件路径集合
        """
        if not self.report_dir.exists():
            return set()

        pattern = f"{report_type}_*.html"
        return set(self.report_dir.glob(pattern))

    def parse_dtc_report(self, report_path: str) -> Dict[str, Any]:
        """
        解析 DTC 报告

        Args:
            report_path: 报告文件路径

        Returns:
            包含车辆信息、模块状态、DTC列表的字典
        """
        self.logger.info(f"Parsing DTC report: {report_path}")

        result = {
            "vehicle_info": {},
            "module_status": [],
            "dtc_list": [],
            "report_path": report_path,
        }

        try:
            with open(report_path, 'r', encoding='iso-8859-1') as f:
                html_content = f.read()

            # 解析车辆信息
            vehicle_info = self._parse_vehicle_info(html_content)
            if vehicle_info:
                result["vehicle_info"] = vehicle_info.to_dict()

            # 解析模块状态
            module_status = self._parse_module_status(html_content)
            result["module_status"] = [m.to_dict() for m in module_status]

            # 解析 DTC 列表
            dtc_list = self._parse_dtc_list(html_content)
            result["dtc_list"] = [d.to_dict() for d in dtc_list]

            self.logger.info(f"Parsed: {len(result['dtc_list'])} DTCs from {len(result['module_status'])} modules")

        except Exception as e:
            self.logger.error(f"Error parsing DTC report: {e}")

        return result

    def _parse_vehicle_info(self, html_content: str) -> Optional[VehicleInfo]:
        """解析车辆信息"""
        try:
            info = VehicleInfo()

            # VIN
            vin_match = re.search(r'Vehicle Identification Number \(VIN\)</td><td>([^<]+)</td>', html_content)
            if vin_match:
                info.vin = vin_match.group(1)

            # Make
            make_match = re.search(r'<td>Make</td><td>([^<]+)</td>', html_content)
            if make_match:
                info.make = make_match.group(1)

            # Model
            model_match = re.search(r'<td>Model</td><td>([^<]+)</td>', html_content)
            if model_match:
                info.model = model_match.group(1)

            # Year
            year_match = re.search(r'<td>Model Year</td><td>([^<]+)</td>', html_content)
            if year_match:
                info.year = year_match.group(1)

            # Transmission
            trans_match = re.search(r'<td>Transmission Type</td><td>([^<]+)</td>', html_content)
            if trans_match:
                info.transmission = trans_match.group(1)

            # Engine
            engine_match = re.search(r'<td>Engine Identifier</td><td>([^<]+)</td>', html_content)
            if engine_match:
                info.engine = engine_match.group(1)

            return info

        except Exception as e:
            self.logger.debug(f"Error parsing vehicle info: {e}")
            return None

    def _parse_module_status(self, html_content: str) -> List[ModuleStatus]:
        """解析模块状态表"""
        modules = []

        try:
            # 模块状态表格模式
            pattern = r'<tr><td>([^<]+)</td><td>(No Communication|Lost Communication|DTCs Stored|OK)</td><td>(\d+)</td><td>([^<]*)</td></tr>'

            matches = re.findall(pattern, html_content)

            for match in matches:
                name, status, dtc_count, dlc_pin = match
                modules.append(ModuleStatus(
                    name=name,
                    status=status,
                    dtc_count=int(dtc_count),
                    dlc_pin=dlc_pin,
                ))

        except Exception as e:
            self.logger.debug(f"Error parsing module status: {e}")

        return modules

    def _parse_dtc_list(self, html_content: str) -> List[DTCInfo]:
        """解析 DTC 列表"""
        dtc_list = []

        try:
            # DTC 表格行模式
            pattern = r'<tr><td>([^<]+)</td><td>([^<]*)</td><td>([A-Z0-9]+)</td><td>([^<]*)</td><td>([^<]+)</td><td>([^<]*)</td><td>([^<]*)<'

            matches = re.findall(pattern, html_content)

            for match in matches:
                module, dtc_type, code, symptom_byte, description, symptom_desc, status = match

                # 跳过表头行
                if code == "DTC Display" or module == "Control Module":
                    continue

                # 提取状态（第一个单词）
                status_clean = status.strip().split('<')[0].strip()

                dtc = DTCInfo(
                    code=code,
                    description=description,
                    symptom_description=symptom_desc if symptom_desc != '- - -' else '',
                    status=status_clean,
                    module=module,
                    symptom_byte=symptom_byte,
                    dtc_type=dtc_type,
                )
                dtc_list.append(dtc)

        except Exception as e:
            self.logger.debug(f"Error parsing DTC list: {e}")

        return dtc_list


# Make DTCReportParser an alias for GDS2ReportParser
DTCReportParser = GDS2ReportParser
