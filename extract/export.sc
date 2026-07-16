@main def exec(cpgFile: String, out: String) = {
  importCpg(cpgFile)
  // methods defined in the repo (skip synthetic <module>/<meta> where useful, keep all for now)
  val methods = cpg.method.map { m =>
    ujson.Obj(
      "fullName" -> m.fullName,
      "name" -> m.name,
      "file" -> m.filename,
      "line" -> m.lineNumber.map(_.toInt).getOrElse(-1)
    )
  }.l

  // call edges: for each call site, caller method -> callee methodFullName (resolved or not)
  val calls = cpg.call.map { c =>
    ujson.Obj(
      "caller" -> c.method.fullName,
      "callerName" -> c.method.name,
      "callee" -> c.methodFullName,
      "calleeName" -> c.name,
      "file" -> c.method.filename,
      "line" -> c.lineNumber.map(_.toInt).getOrElse(-1)
    )
  }.l

  // string literals that look like SQL (rough py-SQL signal)
  val sqlLit = cpg.literal
    .filter(l => l.code.toLowerCase.matches("(?s).*(select |insert into|update |delete from).*"))
    .map { l =>
      ujson.Obj(
        "method" -> l.method.fullName,
        "file" -> l.method.filename,
        "line" -> l.lineNumber.map(_.toInt).getOrElse(-1),
        "code" -> l.code.take(400)
      )
    }.l

  os.write.over(os.Path(out),
    ujson.write(ujson.Obj(
      "methods" -> methods,
      "calls" -> calls,
      "sqlLiterals" -> sqlLit
    ), indent = 2))
  println(s"WROTE ${methods.size} methods, ${calls.size} calls, ${sqlLit.size} sql-literals -> $out")
}
