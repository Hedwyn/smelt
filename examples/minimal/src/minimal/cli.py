import time

import click
from minimal.fib import fib as fib_mypyc
from minimal.fib_cython import fibx as fib_cython


def fib_pure_python(n: int) -> int:
    if n <= 1:
        return n
    else:
        return fib_mypyc(n - 2) + fib_mypyc(n - 1)


@click.group()
def minimal(): ...


@minimal.command()
def say_hello():
    from minimal.hello import hello

    hello()


@minimal.command()
@click.argument("n", type=int)
def compute_fib(n: int):
    for func in [fib_pure_python, fib_mypyc, fib_cython]:
        start_time = time.time()
        click.echo(f"Computing fibonacci of {n} with {func.__name__}")
        click.echo(func(n))
        click.echo(f"Computation took {time.time() - start_time}")


if __name__ == "__main__":
    minimal()
