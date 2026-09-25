package main

import (
	"os"

	"github.com/neusbox/neu_box/client/neubox/internal/cli"
)

func main() {
	os.Exit(cli.Run(os.Args[1:], os.Stdout, os.Stderr))
}
