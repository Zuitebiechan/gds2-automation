# GDS2 Scenic View 手动设置指南

## 重要说明

Scenic View 已经不再积极维护，官方下载链接可能已失效。以下是手动设置的完整步骤。

## 方案A: 使用 Scenic View（如果能找到 JAR）

### 步骤 1: 获取 Scenic View JAR

**选项 1 - 从 GitHub Releases 下载（推荐）:**
1. 访问: https://github.com/JonathanGiles/scenic-view/releases
2. 找到适合 Java 8 的版本（建议 8.8.0 或更早）
3. 下载 `scenic-view-X.X.X.jar` 文件
4. 保存到 `C:\tools\scenic-view.jar`

**选项 2 - 从其他来源:**
- Maven Central: https://search.maven.org/search?q=g:org.scenicview
- 或搜索 "scenic view jar download java 8"

### 步骤 2: 创建启动脚本

创建文件 `C:\tools\GDS2_with_ScenicView.bat`:

```batch
@echo off
REM GDS2 with Scenic View

cd /d "C:\Program Files (x86)\GDS 2"

echo Starting GDS2 with Scenic View...
echo.

"C:\Program Files (x86)\GDS 2\jre6\bin\javaw.exe" ^
  -javaagent:C:\tools\scenic-view.jar ^
  -Xms250m ^
  -Xmx768m ^
  -Dprism.order=sw ^
  -cp RTKApplet.jar;.\itextpdf-5.5.13.1.jar;C:\Program Files (x86)\GM\TIS2WebProxy\dls-nativelibs.jar;C:\Program Files (x86)\GM\TIS2WebProxy\t2w-proxy.jar;C:\Program Files (x86)\GM\TIS2WebProxy\t2w-proxy-impl.jar;C:\Program Files (x86)\GM\TIS2WebProxy\jCookie-0.8c.jar;C:\Program Files (x86)\GM\TIS2WebProxy\jRegistryKey.jar;C:\Program Files (x86)\GM\TIS2WebProxy\log4j-1.2-api-2.17.1.jar;C:\Program Files (x86)\GM\TIS2WebProxy\scsm.jar;TeeChart.Swing.jar;TeeChart.SWT.jar;secdo-public-0.0.1.jar;common-framework-io-0.0.1.jar;commons-lang3-3.9.jar;hamcrest-core-1.3.jar;jackson-annotations-2.10.0.jar;jackson-core-2.10.0.jar;jackson-databind-2.10.0.jar;jackson-dataformat-xml-2.10.0.jar;jackson-module-jaxb-annotations-2.10.0.jar;json-simple-1.1.1.jar;junit-4.12.jar;log4j-api-2.17.1.jar;log4j-core-2.17.1.jar;mockito-all-1.10.19.jar;stax-api-1.0-2.jar;stax2-api-3.1.4.jar;woodstox-core-asl-4.4.1.jar;shared-data-analytics.jar;shared-data-base.jar;shared-data-dtc.jar;shared-data-gds2.jar;shared-data-generic.jar;shared-data-preferences.jar;shared-data-shell.jar;shared-data-vehicle.jar;shared-data-vin.jar;shared-data-system.jar;commons-collections4-4.4.jar;poi-4.1.1.jar;poi-ooxml-4.1.1.jar;poi-ooxml-schemas-4.1.1.jar;xmlbeans-2.6.0.jar; ^
  com.Mahle.Applets.RXMainFX

pause
```

### 步骤 3: 运行测试

1. 关闭正在运行的 GDS2
2. 双击 `C:\tools\GDS2_with_ScenicView.bat`
3. 应该会看到：
   - GDS2 主窗口
   - Scenic View 检查器窗口（如果成功）

### 使用 Scenic View

1. 在 GDS2 中导航到 Data Display 页面
2. 在 Scenic View 中：
   - 左侧显示 UI 组件树
   - 找到 TableView 或 ListView 控件
   - 可以看到所有数据行，不仅仅是可见的
   - 右侧显示属性和数据

---

## 方案B: 使用 Java VisualVM（更可靠的替代方案）

如果 Scenic View 无法工作，可以使用 Java VisualVM：

### 步骤 1: 启动 VisualVM

```bash
# 已包含在 Java JDK 中
"C:\Program Files (x86)\GDS 2\jre6\bin\jvisualvm.exe"
```

### 步骤 2: 连接到 GDS2

1. 启动 GDS2（正常方式）
2. 启动 VisualVM
3. 在左侧找到 GDS2 进程（com.Mahle.Applets.RXMainFX）
4. 双击连接

### 步骤 3: 使用 OQL 查询

VisualVM 可以查询 Java 堆内存：

1. 在 GDS2 中导航到 Data Display 页面
2. 在 VisualVM 中点击 "Heap Dump"
3. 使用 OQL 查询数据：

```sql
-- 查找 TableView
select * from javafx.scene.control.TableView

-- 查找 TableView 的数据
select tv.items from javafx.scene.control.TableView tv

-- 查找所有 Observable List
select * from javafx.collections.ObservableList
```

**优势：**
- 不需要修改启动命令
- 更稳定可靠
- 可以查询任何 Java 对象

**劣势：**
- 需要手动查询，不如 Scenic View 直观
- 需要了解 JavaFX 内部结构

---

## 方案C: 自定义 Java Agent（最可控）

如果上述方案都不行，可以编写自定义 Java Agent：

### 概念：

1. 创建一个 Java Agent JAR
2. 在 Agent 中使用反射访问 JavaFX Scene Graph
3. 定期读取 TableView/ListView 的数据
4. 通过 Socket 或文件输出数据

### 示例代码框架：

```java
public class GDS2DataAgent {
    public static void premain(String args, Instrumentation inst) {
        new Thread(() -> {
            while (true) {
                try {
                    // 获取所有 TableView
                    for (Window window : Window.getWindows()) {
                        Scene scene = window.getScene();
                        TableView table = findTableView(scene.getRoot());
                        if (table != null) {
                            ObservableList items = table.getItems();
                            // 输出数据...
                        }
                    }
                    Thread.sleep(1000);
                } catch (Exception e) {
                    e.printStackTrace();
                }
            }
        }).start();
    }
}
```

这需要 Java 开发知识，但提供了最大的控制力。

---

## 建议

1. **优先尝试方案B (VisualVM)**，因为它最可靠且无需修改启动
2. 如果能找到 Scenic View JAR，方案A 最直观
3. 方案C 需要开发工作，但最灵活

---

## 疑难排查

### Scenic View 不出现？

1. 检查控制台输出是否有错误
2. 确认 JAR 文件兼容 Java 8
3. 尝试其他版本的 Scenic View

### GDS2 无法启动？

1. Agent 可能不兼容
2. 删除 `-javaagent` 参数恢复正常启动
3. 检查 JAR 文件是否损坏

### 数据读取不到？

1. 确认已导航到正确的页面
2. JavaFX 控件可能使用了自定义实现
3. 尝试使用 VisualVM 的 OQL 查询

---

## 总结

**Scenic View 可行性：中等**
- 需要找到兼容的 JAR 文件
- 需要修改启动命令
- 可能存在兼容性问题

**推荐路径：**
1. 先试 VisualVM（方案B）- 最简单可靠
2. 如果不满足需求，再尝试自定义 Agent（方案C）
3. Scenic View（方案A）作为最后尝试

如果你的目标只是读取数据而不是调试 UI，**建议考虑优化现有的 HTML 报告方案**，或者开发自定义 Java Agent。
