package dockerargs

import (
	"reflect"
	"testing"
)

func TestBuildDockerArgsInjectsAnnotation(t *testing.T) {
	got := BuildDockerArgs("sbx_yuxd_12345.slice", []string{"--rm", "-it", "ubuntu", "bash"})
	want := []string{
		"run",
		"--annotation", "sandbox_cgroup=sbx_yuxd_12345.slice",
		"--rm", "-it", "ubuntu", "bash",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %q\nwant %q", got, want)
	}
}

// 只注入 annotation：runtime 不用写，neu-box-runtime 已经是默认 runtime。
func TestBuildDockerArgsInjectsNoRuntime(t *testing.T) {
	got := BuildDockerArgs("sbx_a.slice", []string{"ubuntu"})
	for _, argument := range got {
		if argument == "--runtime" {
			t.Fatalf("--runtime must not be injected: %q", got)
		}
	}
}

// 客户端没有自己的选项：参数一个不改，连开头的 -- 也原样透传。
func TestBuildDockerArgsPassesEverythingThrough(t *testing.T) {
	passthrough := []string{"--", "--sandbox", "--annotation", "sandbox_cgroup=user", "--rm", "ubuntu"}
	got := BuildDockerArgs("sbx_a.slice", passthrough)
	want := append([]string{"run", "--annotation", "sandbox_cgroup=sbx_a.slice"}, passthrough...)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %q\nwant %q", got, want)
	}
}

// 用户自己带了 sandbox_cgroup 也照样注入：要自己写 annotation 的人应该绕开
// neubox 用原生 docker，这里不为那种用法做特殊处理。
func TestBuildDockerArgsInjectsEvenWhenUserHasAnnotation(t *testing.T) {
	got := BuildDockerArgs("sbx_a.slice", []string{"--annotation", "sandbox_cgroup=sbx_other.slice", "ubuntu"})
	want := []string{
		"run",
		"--annotation", "sandbox_cgroup=sbx_a.slice",
		"--annotation", "sandbox_cgroup=sbx_other.slice",
		"ubuntu",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got  %q\nwant %q", got, want)
	}
}

func TestBuildDockerArgsWithoutAnnotationInjectsNothing(t *testing.T) {
	passthrough := []string{"--rm", "ubuntu"}
	got := BuildDockerArgs("", passthrough)
	want := []string{"run", "--rm", "ubuntu"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestBuildDockerArgsWithoutPassthrough(t *testing.T) {
	got := BuildDockerArgs("sbx_a.slice", nil)
	want := []string{"run", "--annotation", "sandbox_cgroup=sbx_a.slice"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestBuildDockerArgsDoesNotAliasPassthrough(t *testing.T) {
	passthrough := []string{"ubuntu"}
	_ = BuildDockerArgs("sbx_a.slice", passthrough)
	if !reflect.DeepEqual(passthrough, []string{"ubuntu"}) {
		t.Fatalf("passthrough mutated: %q", passthrough)
	}
}
