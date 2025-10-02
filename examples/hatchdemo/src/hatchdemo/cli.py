import time

import click

from hatchdemo.fib import fib


@click.group()
def hatchdemo(): ...


@hatchdemo.command()
def say_hello():
    from hatchdemo.hello import hello

    hello()


@hatchdemo.command()
@click.argument("n", type=int)
def compute_fib(n: int):
    start_time = time.time()
    click.echo(f"Computing fibonacci of {n}")
    click.echo(fib(n))
    click.echo(f"Computation took {time.time() - start_time}")


if __name__ == "__main__":
    hatchdemo()
