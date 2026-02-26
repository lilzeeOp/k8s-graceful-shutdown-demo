package main

import (
	"context"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
)

func main() {
	r := gin.Default()

	r.GET("/api/data", func(c *gin.Context) {
		// Simulate work with 100-200ms random sleep
		sleepMs := 100 + rand.Intn(101)
		time.Sleep(time.Duration(sleepMs) * time.Millisecond)

		c.JSON(http.StatusOK, gin.H{
			"source":     "go-upstream",
			"message":    "Hello from Go upstream service",
			"latency_ms": sleepMs,
			"timestamp":  time.Now().Format(time.RFC3339),
		})
	})

	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "healthy"})
	})

	r.GET("/prestop", func(c *gin.Context) {
		log.Println("preStop hook called — starting graceful drain")
		// Sleep to allow K8s to remove this pod from endpoints
		time.Sleep(5 * time.Second)
		log.Println("preStop hook complete — ready for SIGTERM")
		c.JSON(http.StatusOK, gin.H{"status": "drained"})
	})

	graceful := os.Getenv("GRACEFUL")

	if graceful == "true" {
		log.Println("Starting server in GRACEFUL mode on :7000")
		srv := &http.Server{
			Addr:    ":7000",
			Handler: r,
		}

		// Start server in a goroutine
		go func() {
			if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
				log.Fatalf("listen: %s\n", err)
			}
		}()

		// Wait for SIGTERM or SIGINT
		quit := make(chan os.Signal, 1)
		signal.Notify(quit, syscall.SIGTERM, syscall.SIGINT)
		sig := <-quit
		log.Printf("Received signal %v — shutting down gracefully...\n", sig)

		// Give in-flight requests up to 15s to complete
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()

		if err := srv.Shutdown(ctx); err != nil {
			log.Fatalf("Server forced to shutdown: %v\n", err)
		}
		log.Println("Server exited gracefully")
	} else {
		log.Println("Starting server in NON-GRACEFUL mode on :7000")
		fmt.Println("(No signal handling — will terminate abruptly on SIGTERM)")
		if err := r.Run(":7000"); err != nil {
			log.Fatalf("Failed to start server: %v\n", err)
		}
	}
}
