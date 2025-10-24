---
title: "What’s New in Java 25 (JDK 25)"
description: "A developer-friendly tour of the most important changes in Java 25 (LTS): language improvements, concurrency, performance work, JFR tooling, AOT ergonomics, GC updates, and more."
date: 2025-10-24
tags: ["java", "jdk-25", "lts"]
categories: ["technology"]
---

Java 25 (JDK 25) is here — and it’s an LTS release. That means most vendors will support it for years, and many teams will plan upgrades from JDK 17 or 21 directly to 25. In this post, I’ll walk through the highlights that matter in real projects: language changes, concurrency improvements (Project Loom), runtime and GC work, observability via JFR, security/crypto, and AOT ergonomics.

This post is based on the official OpenJDK pages and JEPs for JDK 25.

---

## Quick Summary

- **LTS release with 18 JEPs**. See the official list below.
- **Language & syntax**: Primitive types in patterns/switch (3rd preview), module import declarations, compact source files & instance main methods, flexible constructor bodies.
- **Concurrency (Loom)**: Structured Concurrency (5th preview), Scoped Values (finalized).
- **Observability (JFR)**: CPU-time profiling (experimental), method timing & tracing, cooperative sampling.
- **Performance & memory**: Compact object headers, Generational Shenandoah GC.
- **Compute/SIMD**: Vector API reaches its 10th incubator.
- **Security & crypto**: PEM encodings (preview), Key Derivation Function API.

---

## Language and Syntax Improvements

- **Primitive Types in Patterns, instanceof, and switch (Third Preview)** — JEP 507
  - Brings pattern matching to primitives, improving expressiveness in `switch` and `instanceof` scenarios.
  - Still a preview: enable with `--enable-preview` while evaluating in production-like tests.

  ```java
  Object x = 42;
  
  // instanceof with a primitive pattern
  if (x instanceof int i) {
      System.out.println(i + 1);
  }
  
  // switch with primitive patterns
  switch (x) {
      case int i -> System.out.println("int: " + i);
      case long l -> System.out.println("long: " + l);
      case double d -> System.out.println("double: " + d);
      default -> System.out.println("other: " + x);
  }
  ```

- **Module Import Declarations** — JEP 511
  - Streamlines how code refers to modules and their content, complementing package-level imports.

  ```java
  // Import all exported packages from the java.sql module
  import module java.sql;
  
  // Then regular package imports work as usual
  import java.sql.Connection;
  import java.sql.DriverManager;
  
  class Demo {
      void main() {
          try (Connection conn = DriverManager.getConnection(
                  "jdbc:postgresql://localhost/db", "u", "p")) {
              System.out.println(conn.getMetaData().getURL());
          } catch (Exception e) {
              e.printStackTrace();
          }
      }
  }
  ```

- **Compact Source Files and Instance Main Methods** — JEP 512
  - Makes small programs and examples easier to write and run with a leaner source file format and support for instance `main` methods.

  ```java
  // Instance main method (no public/static/String[] args)
  class HelloWorld {
      void main() {
          System.out.println("Hello, World!");
      }
  }
  ```

  ```bash
  java HelloWorld.java
  ```

- **Flexible Constructor Bodies** — JEP 513
  - Eases constraints in constructors, improving readability and reducing boilerplate in common patterns.

  ```java
  class A {
      A(int x) { System.out.println("A(" + x + ")"); }
  }
  
  class B extends A {
      private final int y;
  
      B(int y) {
          // Prologue: statements before the explicit constructor invocation
          if (y < 0) throw new IllegalArgumentException("y must be >= 0");
          System.out.println("Preparing...");
  
          // Explicit superclass constructor call
          super(y);
  
          // Epilogue: statements after the invocation
          this.y = y;
          System.out.println("Done.");
          return; // allowed in the epilogue (no expression)
      }
  }
  ```

---

## Concurrency & Loom

- **Structured Concurrency (Fifth Preview)** — JEP 505
  - A higher-level API for managing related tasks as a single unit, leading to clearer code and predictable cancellation/error handling.
  - Preview status lets the API evolve with real-world feedback.

  ```java
  import java.util.concurrent.StructuredTaskScope;
  
  String render() throws Exception {
      try (var scope = StructuredTaskScope.<String, Void>open()) {
          var userTask = scope.fork(() -> fetchUser());
          var ordersTask = scope.fork(() -> fetchOrders());
          scope.join(); // wait for both subtasks to complete
          return "%s | %s".formatted(userTask.get(), ordersTask.get());
      }
  }
  ```

- **Scoped Values** — JEP 506
  - Finalized. Provides a safe, immutable, thread-local-like mechanism for sharing data within a call scope, especially effective with virtual threads.

  ```java
  // Define a scoped value
  static final java.lang.ScopedValue<String> REQUEST_ID = java.lang.ScopedValue.newInstance();
  
  void handle(Request req) {
      java.lang.ScopedValue.where(REQUEST_ID, req.id()).run(() -> {
          service();
      });
  }
  
  void service() {
      System.out.println("rid=" + REQUEST_ID.get());
  }
  ```

---

## Observability with JFR

- **JFR CPU-Time Profiling (Experimental)** — JEP 509
  - Adds better attribution of CPU time to Java code paths for more accurate performance analysis.
- **JFR Method Timing & Tracing** — JEP 520
  - Richer, lower-overhead method-level timing and trace events to diagnose latency and hotspots.
- **JFR Cooperative Sampling** — JEP 518
  - Improves sampling behavior and consistency across runtimes.

  ```bash
  # Start a JFR recording and dump on exit
  java -XX:StartFlightRecording=filename=profile.jfr,dumponexit=true -jar app.jar
  
  # View CPU-time hot methods (new view in JDK 25)
  jfr view cpu-time-hot-methods profile.jfr
  ```

  ```bash
  # Record method timing for static initializers and dump to file
  java '-XX:StartFlightRecording:method-timing=::<clinit>,filename=clinit.jfr' -jar app.jar
  
  # View method timing results
  jfr view method-timing clinit.jfr
  ```

---

## Performance, Memory, and GC

- **Compact Object Headers** — JEP 519
  - Reduces object header size on supported platforms to lower memory footprint and improve cache locality.
 
 - **Generational Shenandoah** — JEP 521
   - Introduces a generational mode for Shenandoah GC, improving throughput and latency for long-lived services.
 
   ```bash
   # Enable Shenandoah in generational mode
   java -XX:+UseShenandoahGC -XX:ShenandoahGCMode=generational -jar app.jar
   ```

- **Remove the 32-bit x86 Port** — JEP 503
  - Cleans up legacy maintenance burden; modern Java focuses on x64 and ARM64.

---

## AOT Ergonomics and Profiling

- **Ahead-of-Time Command-Line Ergonomics** — JEP 514
  - Smoother CLI options and defaults when experimenting with AOT builds.
- **Ahead-of-Time Method Profiling** — JEP 515
  - Helps guide AOT compilation decisions with targeted profiling data.

```bash
# One-step training + cache creation
java -XX:AOTCacheOutput=app.aot -cp app.jar com.example.App

# Production run using the AOT cache
java -XX:AOTCache=app.aot -cp app.jar com.example.App
```

---

## Vector API (Incubator)

- **Vector API (Tenth Incubator)** — JEP 508
  - Continued refinement of a portable, explicit SIMD API for data-parallel operations, mapping efficiently to modern CPU instructions.
  - As an incubator, it remains behind `--add-modules jdk.incubator.vector` for now.

  ```java
  import jdk.incubator.vector.*;

  static final VectorSpecies<Float> SPECIES = FloatVector.SPECIES_PREFERRED;

  void vectorCompute(float[] a, float[] b, float[] c) {
      int i = 0;
      int ub = SPECIES.loopBound(a.length);
      for (; i < ub; i += SPECIES.length()) {
          var va = FloatVector.fromArray(SPECIES, a, i);
          var vb = FloatVector.fromArray(SPECIES, b, i);
          var vc = va.mul(va).add(vb.mul(vb)).neg();
          vc.intoArray(c, i);
      }
      for (; i < a.length; i++) {
          c[i] = -(a[i] * a[i] + b[i] * b[i]);
      }
  }
  ```

  ```bash
  # Run with the incubator module
  java --add-modules jdk.incubator.vector VectorDemo
  ```

---

## Security & Cryptography

- **PEM Encodings of Cryptographic Objects (Preview)** — JEP 470
  - First-class support for reading/writing PEM-encoded material, simplifying interoperability with common tooling.
- **Key Derivation Function API** — JEP 510
  - Standardized APIs for modern KDFs, making secure key derivation more approachable and consistent.

```java
// KDF (HKDF-SHA256) example — derive a 32-byte AES key
import javax.crypto.KDF;
import javax.crypto.SecretKey;
import java.security.spec.AlgorithmParameterSpec;
import javax.crypto.spec.HKDFParameterSpec;

byte[] initialKeyMaterial = /* ... */ null;
byte[] salt = /* ... */ null;
byte[] info = /* ... */ null;

KDF hkdf = KDF.getInstance("HKDF-SHA256");
AlgorithmParameterSpec params = HKDFParameterSpec.ofExtract()
    .addIKM(initialKeyMaterial)
    .addSalt(salt)
    .thenExpand(info, 32);
SecretKey key = hkdf.deriveKey("AES", params);
```

```java
// PEM encode/decode (Preview)
import java.security.PEMEncoder;
import java.security.PEMDecoder;
import java.security.KeyPair;
import java.security.interfaces.ECPublicKey;

// Encode a key pair as PEM text
var pe = PEMEncoder.of();
String pem = pe.encodeToString(new KeyPair(publicKey, privateKey));

// Decode with the expected type
var pd = PEMDecoder.of();
ECPublicKey pub = pd.decode(pem, ECPublicKey.class);
```

---

## References

- JDK 25 overview: https://openjdk.org/projects/jdk/25/
- JDK 25 feature list (JEPs): https://openjdk.org/projects/jdk/25/
- JEP index: https://openjdk.org/jeps/0

---

## Conclusion

JDK 25 is a solid LTS that continues the steady modernization of Java: safer and clearer concurrency, better profiling, smarter memory layout, and practical language polish. If you’re on JDK 17 or 21, this is a compelling target for your next upgrade window — especially if you benefit from Loom, JFR improvements, or reduced memory overhead.
