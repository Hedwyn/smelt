import time

import click

from minimal.fib import fib


@click.group()
def minimal(): ...


@minimal.command()
def say_hello():
    from minimal.hello import hello

    hello()


@minimal.command()
@click.argument("n", type=int)
def compute_fib(n: int):
    start_time = time.time()
    click.echo(f"Computing fibonacci of {n}")
    click.echo(fib(n))
    click.echo(f"Computation took {time.time() - start_time}")


if __name__ == "__main__":
    minimal()
