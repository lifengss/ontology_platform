import org.apache.jena.graph.Graph;
import org.apache.jena.riot.Lang;
import org.apache.jena.riot.RDFDataMgr;
import org.apache.jena.riot.RDFParser;
import org.apache.jena.shacl.ShaclValidator;
import org.apache.jena.shacl.ValidationReport;

/**
 * Jena 侧 SHACL 校验执行器（V2-17）。
 *
 * 用法（JDK 11+ 单文件源启动，无需 javac）：
 *   java -cp fuseki-server.jar ShaclEval.java <shapes.nt> <data.nt>
 * 输出：第一行 CONFORMS=true|false，其后为 SHACL 校验报告图（N-Triples，
 * 含 sh:ValidationResult 三元组），由调用方（Python）解析为统一违规明细。
 *
 * 引擎：org.apache.jena.shacl（Jena 5.1.0 fat jar 内置，与 Fuseki 推理服务同一
 * Jena/RDF 栈）；形状与数据文件与本地 pyshacl 侧完全相同（形状版本一致）。
 */
public class ShaclEval {
    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("usage: ShaclEval <shapes.nt> <data.nt>");
            System.exit(2);
        }
        Graph shapes = RDFParser.source(args[0]).toGraph();
        Graph data = RDFParser.source(args[1]).toGraph();
        ValidationReport report = ShaclValidator.get().validate(shapes, data);
        System.out.println("CONFORMS=" + report.conforms());
        RDFDataMgr.write(System.out, report.getGraph(), Lang.NTRIPLES);
    }
}
