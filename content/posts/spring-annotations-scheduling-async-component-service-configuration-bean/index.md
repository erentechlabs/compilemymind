---
title: "Spring Annotations Guide: @EnableScheduling, @Scheduled, @Async, @EnableAsync, @Service, and @Configuration Examples"
description: "A practical guide to core Spring annotations with concise examples: scheduling tasks, async methods, component stereotypes, and Java-based configuration."
date: 2025-10-24
tags: ["java", "spring", "spring-boot", "spring-annotations"]
categories: ["technology"]
---

This post explains some of the most useful Spring annotations with minimal code you can paste into a project. We’ll cover what each annotation does, when to use it, and a short example.

---

## @EnableScheduling and @Scheduled

- **What they do**
  - **@EnableScheduling**: Turns on Spring’s scheduled task processing.
  - **@Scheduled**: Marks a method to run on a schedule.

```java
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Configuration
@EnableScheduling
class SchedulingConfig { }

@Component
class ReportScheduler {
    // Runs every 5 seconds after previous start
    @Scheduled(fixedRate = 5_000)
    public void exportSummary() {
        System.out.println("exportSummary @ " + java.time.Instant.now());
    }

    // Cron: 09:00 Monday–Friday in Europe/Istanbul
    @Scheduled(cron = "0 0 9 * * MON-FRI", zone = "Europe/Istanbul")
    public void morningDigest() {
        System.out.println("morningDigest @ " + java.time.Instant.now());
    }
}
```

---

## @EnableAsync and @Async

- **What they do**
  - **@EnableAsync**: Enables Spring’s async method execution infrastructure.
  - **@Async**: Runs a method on a separate thread (from a configured executor).

```java
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.annotation.Async;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.stereotype.Service;

import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;

@Configuration
@EnableAsync
class AsyncConfig {
    @Bean(name = "taskExecutor")
    Executor taskExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(4);
        executor.setMaxPoolSize(8);
        executor.setQueueCapacity(100);
        executor.setThreadNamePrefix("async-");
        executor.initialize();
        return executor;
    }
}

@Service
class EmailService {
    @Async("taskExecutor")
    public CompletableFuture<Void> sendWelcomeEmail(String to) {
        try {
            // simulate I/O
            Thread.sleep(500);
            System.out.println("sent welcome email to: " + to);
            return CompletableFuture.completedFuture(null);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return CompletableFuture.failedFuture(e);
        }
    }
}
```

---

## @Component and @Service

- **What they do**
  - **@Component**: Generic stereotype to mark a class for component scanning.
  - **@Service**: Specialization of `@Component`, semantically indicating business logic.

```java
import org.springframework.stereotype.Component;
import org.springframework.stereotype.Service;

@Component
class SlugGenerator {
    String toSlug(String title) {
        return title.toLowerCase().replaceAll("[^a-z0-9]+", "-").replaceAll("(^-|-$)", "");
    }
}

@Service
class BlogService {
    private final SlugGenerator slugGenerator;

    BlogService(SlugGenerator slugGenerator) {
        this.slugGenerator = slugGenerator;
    }

    String createSlug(String title) {
        return slugGenerator.toSlug(title);
    }
}
```

---

## @Configuration and @Bean

- **What they do**
  - **@Configuration**: Declares a class that defines beans via `@Bean` methods.
  - **@Bean**: Declares a Spring bean returned by the annotated method.

```java
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.stereotype.Service;

import java.time.Clock;

@Configuration
class AppConfig {
    @Bean
    Clock clock() {
        return java.time.Clock.systemUTC();
    }
}

@Service
class TimeService {
    private final Clock clock;

    TimeService(Clock clock) {
        this.clock = clock;
    }

    java.time.Instant now() {
        return java.time.Instant.now(clock);
    }
}
```

---

## Tips and gotchas

- **@Async self-invocation**: Calls within the same class won’t go through the proxy, so `@Async` won’t take effect. Call from another Spring bean.
- **@Async return types**: Prefer `CompletableFuture<T>` for error handling. For `void`, use an `AsyncUncaughtExceptionHandler` if you need to observe errors.
- **Scheduling threads**: Default scheduler is limited. For parallel schedules, define a `TaskScheduler` bean or use `ThreadPoolTaskScheduler`.
- **Cron time zone**: Always set the `zone` attribute if your business time zone differs from server time.
- **@Configuration vs @Component**: Use `@Configuration` for full configuration (ensures `@Bean` methods return managed singletons even when called within the class).
